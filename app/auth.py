"""Accounts, logins and who may see or change what.

The first account (made on the first visit) is the main admin and owns everything that
wasn't uploaded by another user. Admins make the other accounts; there is no sign-up.
Each user sees their own models and everyone's public ones; an admin in admin mode sees
every model. Only a model's owner, or an admin, can change it.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sys
import time
from dataclasses import dataclass

from . import db

COOKIE = "rmm_session"
SESSION_DAYS = 365
_ITERATIONS = 240_000
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


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

    @property
    def sees_all(self) -> bool:
        return self.is_admin and self.admin_mode

    def can_edit(self, owner: int) -> bool:
        return self.is_admin or owner == self.id

    def as_dict(self) -> dict:
        return {"id": self.id, "username": self.username, "is_admin": self.is_admin,
                "admin_mode": self.sees_all, "default_public": self.default_public,
                "main_admin": self.id == db.MAIN_ADMIN}


def setup_needed() -> bool:
    return not db.conn().execute("SELECT 1 FROM users LIMIT 1").fetchone()


def create_user(username: str, password: str, is_admin: bool = False) -> int:
    c = db.conn()
    username, password = check_username(username), check_new_password(password)
    if c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise ValueError(f"There is already a user called {username}")
    first = setup_needed()
    cur = c.execute("INSERT INTO users(id, username, password, is_admin) VALUES (?, ?, ?, ?)",
                    (db.MAIN_ADMIN if first else None, username, hash_password(password), int(is_admin or first)))
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


def new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    c = db.conn()
    c.execute("DELETE FROM sessions WHERE seen < ?", (now - SESSION_DAYS * 86400,))
    c.execute("INSERT INTO sessions(token, user_id, created, seen) VALUES (?, ?, ?, ?)",
              (_token_key(token), user_id, now, now))
    c.commit()
    return token


def logout(token: str):
    db.conn().execute("DELETE FROM sessions WHERE token = ?", (_token_key(token),))
    db.conn().commit()


def viewer(token: str | None) -> Viewer | None:
    if not token:
        return None
    c = db.conn()
    row = c.execute("""SELECT u.id, u.username, u.is_admin, u.default_public, s.admin_mode, s.seen
                       FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?""",
                    (_token_key(token),)).fetchone()
    if not row or row["seen"] < time.time() - SESSION_DAYS * 86400:
        return None
    if row["seen"] < time.time() - 3600:  # keeps the session alive while it is used
        c.execute("UPDATE sessions SET seen = ? WHERE token = ?", (time.time(), _token_key(token)))
        c.commit()
    return Viewer(row["id"], row["username"], bool(row["is_admin"]), bool(row["admin_mode"]), bool(row["default_public"]))


def set_admin_mode(token: str, on: bool):
    db.conn().execute("UPDATE sessions SET admin_mode = ? WHERE token = ?", (int(on), _token_key(token)))
    db.conn().commit()


def users() -> list[dict]:
    return [{"id": r["id"], "username": r["username"], "is_admin": bool(r["is_admin"]),
             "default_public": bool(r["default_public"]), "main_admin": r["id"] == db.MAIN_ADMIN}
            for r in db.conn().execute("SELECT * FROM users ORDER BY id")]


def usernames() -> dict[int, str]:
    return {r["id"]: r["username"] for r in db.conn().execute("SELECT id, username FROM users")}


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
    """Remove an account. Their files stay in the library and belong to the main admin from then on."""
    if user_id == db.MAIN_ADMIN:
        raise ValueError("The main admin can't be removed")
    c = db.conn()
    if not c.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        return False
    c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
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
