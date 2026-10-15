"""Accounts, logins and who may see or change what.

The first account (made on the first visit) is the main admin and owns everything that
wasn't uploaded by another user. Admins make the other accounts; there is no sign-up.
Each user sees their own models and everyone's public ones; an admin in admin mode sees
every model. Only a model's owner, or an admin, can change it.

With config.AUTH_PROXY on, a reverse proxy with forward auth (Authelia and the like) can sign
people in instead: it passes on their username in a header, and their account is made the
first time they come. Local logins keep working alongside.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import socket
import sys
import time
from dataclasses import dataclass

from . import config, db, library

COOKIE = "rmm_session"
SESSION_DAYS = 365
_ITERATIONS = 240_000
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
# Stored for accounts made by the reverse proxy: matches no password, so they can't log in
# locally until an admin sets one.
NO_PASSWORD = "!"
log = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS).hex()
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${h}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    except ValueError:
        return False
    return hmac.compare_digest(got, h)


def check_username(name: str) -> str:
    name = name.strip()
    if not USERNAME.match(name):
        raise ValueError("A username is 1 to 32 letters, digits, dots, dashes or underscores")
    if name.casefold() == db.REMOVED_NAME:
        raise ValueError(f"{name} is kept for the models of removed accounts")
    return name


def check_new_password(password: str) -> str:
    if len(password) < 6:
        raise ValueError("Use a password of at least 6 characters")
    return password


@dataclass
class Viewer:
    """The logged-in user, as far as a request is concerned."""
    id: int
    username: str
    is_admin: bool
    admin_mode: bool  # an admin seeing every model, private ones too
    default_public: bool
    folder: str  # where their uploads go; '' = the library itself (the main admin)
    proxy: bool = False  # signed in through the reverse proxy, which also handles logging out

    @property
    def sees_all(self) -> bool:
        return self.is_admin and self.admin_mode

    def can_edit(self, owner: int) -> bool:
        return self.is_admin or owner == self.id

    def as_dict(self) -> dict:
        return {"id": self.id, "username": self.username, "is_admin": self.is_admin,
                "admin_mode": self.sees_all, "default_public": self.default_public, "folder": self.folder,
                "main_admin": self.id == db.MAIN_ADMIN, "proxy": self.proxy}


def setup_needed() -> bool:
    return not db.conn().execute("SELECT 1 FROM users LIMIT 1").fetchone()


def create_user(username: str, password: str | None, is_admin: bool = False) -> int:
    """A new account; password None for one signed in by the reverse proxy."""
    c = db.conn()
    username = check_username(username)
    hashed = NO_PASSWORD if password is None else hash_password(check_new_password(password))
    if c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise ValueError(f"There is already a user called {username}")
    first = setup_needed()
    folder = "" if first else library.new_user_folder(username)  # the main admin's uploads go in the library itself
    cur = c.execute("INSERT INTO users(id, username, password, is_admin, folder) VALUES (?, ?, ?, ?, ?)",
                    (db.MAIN_ADMIN if first else None, username, hashed, int(is_admin or first), folder))
    if folder:
        c.execute("INSERT INTO folder_owners(folder, owner) VALUES (?, ?)", (folder, cur.lastrowid))
    c.commit()
    return cur.lastrowid


def login(username: str, password: str) -> str | None:
    """A new session token for the right password, else None."""
    row = db.conn().execute("SELECT id, password FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if not row or not check_password(password, row["password"]):
        time.sleep(1)  # slows down guessing
        return None
    return new_session(row["id"])


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id: int, proxy: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    c = db.conn()
    c.execute("DELETE FROM sessions WHERE seen < ?", (now - SESSION_DAYS * 86400,))
    c.execute("INSERT INTO sessions(token, user_id, proxy, created, seen) VALUES (?, ?, ?, ?, ?)",
              (_token_key(token), user_id, int(proxy), now, now))
    c.commit()
    return token


def logout(token: str):
    db.conn().execute("DELETE FROM sessions WHERE token = ?", (_token_key(token),))
    db.conn().commit()


def viewer(token: str | None) -> Viewer | None:
    if not token:
        return None
    c = db.conn()
    row = c.execute("""SELECT u.id, u.username, u.is_admin, u.default_public, u.folder, s.admin_mode, s.proxy, s.seen
                       FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?""",
                    (_token_key(token),)).fetchone()
    if not row or row["seen"] < time.time() - SESSION_DAYS * 86400:
        return None
    if row["seen"] < time.time() - 3600:  # keeps the session alive while it is used
        c.execute("UPDATE sessions SET seen = ? WHERE token = ?", (time.time(), _token_key(token)))
        c.commit()
    return Viewer(row["id"], row["username"], bool(row["is_admin"]), bool(row["admin_mode"]),
                  bool(row["default_public"]), row["folder"], bool(row["proxy"]))


# ---------------------------------------------------------------- reverse proxy sign-in

_resolved: dict[str, tuple[float, set[str]]] = {}  # host name -> (when, its addresses)


def _addresses(host: str) -> set[str]:
    when, ips = _resolved.get(host, (0.0, set()))
    if time.time() - when > 60:  # a proxy container can get a new address when it restarts
        try:
            ips = {a[4][0] for a in socket.getaddrinfo(host, None)}
        except OSError:
            ips = set()
        _resolved[host] = (time.time(), ips)
    return ips


def trusted_proxy(ip: str) -> bool:
    """Whether a request from `ip` may say who is signed in (config.AUTH_PROXY_TRUSTED)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    for entry in config.AUTH_PROXY_TRUSTED:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
            continue
        except ValueError:
            pass  # a host name
        if str(addr) in {str(ipaddress.ip_address(a.split("%")[0])) for a in _addresses(entry)}:
            return True
    return False


def proxy_login(username: str, groups: set[str], token: str | None) -> tuple[Viewer, str | None]:
    """The user the reverse proxy vouches for, with their account made on their first visit and
    admin rights following config.AUTH_PROXY_ADMIN_GROUP. Returns their viewer and, when the
    session in `token` isn't theirs, the token of a new one."""
    username = check_username(username)
    c = db.conn()
    admin = config.AUTH_PROXY_ADMIN_GROUP in groups if config.AUTH_PROXY_ADMIN_GROUP else None
    row = c.execute("SELECT id, is_admin FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        user_id = create_user(username, None, bool(admin))
        log.info("Made an account for %s, signed in by the reverse proxy", username)
    else:
        user_id = row["id"]
        if admin is not None and admin != bool(row["is_admin"]) and user_id != db.MAIN_ADMIN:
            update_user(user_id, is_admin=admin)
    v = viewer(token)
    if v and v.id == user_id:
        return v, None
    token = new_session(user_id, proxy=True)
    return viewer(token), token


def set_admin_mode(token: str, on: bool):
    db.conn().execute("UPDATE sessions SET admin_mode = ? WHERE token = ?", (int(on), _token_key(token)))
    db.conn().commit()


def users() -> list[dict]:
    return [{"id": r["id"], "username": r["username"], "is_admin": bool(r["is_admin"]),
             "default_public": bool(r["default_public"]), "main_admin": r["id"] == db.MAIN_ADMIN}
            for r in db.conn().execute("SELECT * FROM users ORDER BY id")]


def usernames() -> dict[int, str]:
    """user id -> name, with removed accounts' models under db.REMOVED_NAME."""
    return {db.REMOVED_USER: db.REMOVED_NAME,
            **{r["id"]: r["username"] for r in db.conn().execute("SELECT id, username FROM users")}}


def set_password(user_id: int, password: str, end_sessions: bool = True, keep: str | None = None):
    """New password; the user's other sessions (all but `keep`) are logged out."""
    c = db.conn()
    c.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(check_new_password(password)), user_id))
    if end_sessions:
        c.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user_id, _token_key(keep) if keep else ""))
    c.commit()


def update_user(user_id: int, is_admin: bool | None = None, default_public: bool | None = None):
    c = db.conn()
    if is_admin is not None:
        if user_id == db.MAIN_ADMIN and not is_admin:
            raise ValueError("The main admin stays an admin")
        c.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
        if not is_admin:
            c.execute("UPDATE sessions SET admin_mode = 0 WHERE user_id = ?", (user_id,))
    if default_public is not None:
        c.execute("UPDATE users SET default_public = ? WHERE id = ?", (int(default_public), user_id))
    c.commit()


def delete_user(user_id: int) -> bool:
    """Remove an account. Their files stay in the library, owned by db.REMOVED_USER until an admin
    claims them (library.claim)."""
    if user_id == db.MAIN_ADMIN:
        raise ValueError("The main admin can't be removed")
    c = db.conn()
    if not c.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        return False
    c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    c.execute("UPDATE folder_owners SET owner = ? WHERE owner = ?", (db.REMOVED_USER, user_id))
    c.commit()
    return True


def main(argv: list[str]):
    """Reset a password from the command line, e.g. when the admin password is forgotten:
    docker exec -it resin-models python -m app.auth reset-password <username>"""
    import getpass
    if len(argv) != 2 or argv[0] != "reset-password":
        raise SystemExit("usage: python -m app.auth reset-password <username>")
    db.init()
    row = db.conn().execute("SELECT id FROM users WHERE username = ?", (argv[1],)).fetchone()
    if not row:
        raise SystemExit(f"No user called {argv[1]}")
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Again: "):
        raise SystemExit("The passwords don't match")
    set_password(row["id"], password)
    print(f"Password changed for {argv[1]}; their sessions were logged out.")


if __name__ == "__main__":
    main(sys.argv[1:])
