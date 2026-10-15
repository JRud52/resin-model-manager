"""Signing in through a reverse proxy with forward auth (Authelia): the Remote-User header.

    AUTH_PROXY=1 AUTH_PROXY_TRUSTED=127.0.0.1 AUTH_PROXY_ADMIN_GROUP=admins \\
    AUTH_PROXY_LOGOUT_URL=https://auth.example.com/logout uvicorn app.main:app --port 8417
    python tests/proxy_auth_test.py http://localhost:8417
"""
import http.cookiejar
import json
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from login import ADMIN, PASSWORD, login  # noqa: E402

BASE = sys.argv[1].rstrip("/")


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, u, body=None, user=None, groups=None):
        headers = {"Content-Type": "application/json"}
        if user is not None:
            headers["Remote-User"] = user
        if groups is not None:
            headers["Remote-Groups"] = groups
        req = urllib.request.Request(BASE + u, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            r = self.opener.open(req)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


login(BASE)  # the main admin exists, so proxy accounts are ordinary ones
name = f"sso-{secrets.token_hex(3)}"

c = Client()
assert c.call("GET", "/api/me")[0] == 401, "no header, no cookie: logged out"
status, me = c.call("GET", "/api/me", user=name, groups="family")
assert status == 200 and me["username"] == name and me["proxy"] and not me["is_admin"], me
assert any(k.name == "rmm_session" for k in c.jar), "the proxy sign-in sets a session cookie"
assert c.call("GET", "/api/me", user=name.upper())[1]["id"] == me["id"], "usernames match ignoring case"
assert c.call("GET", "/api/me", user=name, groups="family, admins")[1]["is_admin"], "the admin group makes an admin"
assert not c.call("GET", "/api/me", user=name, groups="family")[1]["is_admin"], "and leaving it takes that away"
assert c.call("GET", "/api/me")[0] == 401, "a proxy session needs the header on every request"
assert c.call("PUT", "/api/me/admin-mode", {"on": True}, user=name)[0] == 403, "not an admin"

# Admin mode belongs to the session, which carries on from request to request.
c.call("GET", "/api/me", user=name, groups="admins")
assert c.call("PUT", "/api/me/admin-mode", {"on": True}, user=name, groups="admins")[1]["admin_mode"]
assert c.call("GET", "/api/me", user=name, groups="admins")[1]["admin_mode"]

# Another person through the same browser gets their own session.
other = f"{name}-b"
status, me2 = c.call("GET", "/api/me", user=other)
assert status == 200 and me2["username"] == other and me2["id"] != me["id"]

assert c.call("GET", "/api/me", user="not ok!")[0] == 403, "a username the app can't use is refused"
assert c.call("GET", "/api/status", user="not ok!")[0] == 403

# The local admin's name signs in to that account; it isn't proxy-made, but this session is.
status, admin = c.call("GET", "/api/me", user=ADMIN)
assert admin["main_admin"] and admin["is_admin"] and admin["proxy"], admin

status, out = c.call("POST", "/api/auth/logout", user=name)
assert out["redirect"] == "https://auth.example.com/logout", out

# Local logins keep working alongside, without a redirect on logout.
local = Client()
assert local.call("POST", "/api/auth/login", {"username": ADMIN, "password": PASSWORD})[0] == 200
status, me = local.call("GET", "/api/me")
assert status == 200 and not me["proxy"]
assert local.call("POST", "/api/auth/logout")[1]["redirect"] == ""
assert local.call("POST", "/api/auth/login", {"username": name, "password": "!"})[0] == 401, "no password of their own"

# Who counts as the proxy.
from app import auth, config  # noqa: E402
config.AUTH_PROXY_TRUSTED = ["10.1.0.0/16", "localhost", "192.168.1.5"]
assert auth.trusted_proxy("10.1.2.3") and auth.trusted_proxy("192.168.1.5") and auth.trusted_proxy("127.0.0.1")
assert auth.trusted_proxy("::ffff:10.1.2.3")
assert not auth.trusted_proxy("10.2.0.1") and not auth.trusted_proxy("192.168.1.6") and not auth.trusted_proxy("")
config.AUTH_PROXY_TRUSTED = []
assert not auth.trusted_proxy("127.0.0.1"), "nothing trusted unless configured"
print("proxy auth ok")
