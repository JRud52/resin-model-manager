"""Sign in with Authelia through OpenID Connect, against a fake provider this test runs itself.

    OIDC_ISSUER=http://127.0.0.1:8419 OIDC_CLIENT_ID=models OIDC_CLIENT_SECRET=s3cret \\
    OIDC_ADMIN_GROUP=admins uvicorn app.main:app --port 8417
    python tests/oidc_test.py http://localhost:8417
"""
import base64
import hashlib
import http.cookiejar
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from login import ADMIN, PASSWORD, login  # noqa: E402

BASE = sys.argv[1].rstrip("/")
ISSUER = "http://127.0.0.1:8419"
CLIENT, SECRET = "models", "s3cret"

# Who the fake provider signs in next, and how it misbehaves.
user = {"name": "", "groups": [], "aud": CLIENT, "nonce": None}
codes: dict[str, dict] = {}


def _jwt(claims: dict) -> str:
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


class Provider(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/.well-known/openid-configuration":
            return self._json({"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                               "token_endpoint": ISSUER + "/token", "userinfo_endpoint": ISSUER + "/userinfo"})
        if url.path == "/authorize":  # signs `user` in at once
            assert q["client_id"] == CLIENT and q["code_challenge_method"] == "S256" and "groups" in q["scope"]
            code = secrets.token_hex(8)
            codes[code] = {**q, "user": dict(user)}
            self.send_response(302)
            self.send_header("Location", q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]}))
            self.end_headers()
            return
        if url.path == "/userinfo":
            token = self.headers["Authorization"].split()[1]
            u = codes[token]["user"]
            return self._json({"sub": "id-" + u["name"], "preferred_username": u["name"], "groups": u["groups"]})
        self._json({}, 404)

    def do_POST(self):
        body = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
        if base64.b64decode(self.headers["Authorization"].split()[1]).decode() != f"{CLIENT}:{SECRET}":
            return self._json({"error": "invalid_client"}, 401)
        pending = codes.get(body["code"])
        verifier = body["code_verifier"].encode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=").decode()
        if not pending or challenge != pending["code_challenge"] or body["redirect_uri"] != pending["redirect_uri"]:
            return self._json({"error": "invalid_grant"}, 400)
        u = pending["user"]
        codes[body["code"]] = pending  # the code doubles as the access token here
        return self._json({"access_token": body["code"], "token_type": "Bearer", "id_token": _jwt({
            "iss": ISSUER, "aud": [u["aud"]], "sub": "id-" + u["name"], "exp": time.time() + 300,
            "nonce": u["nonce"] or pending["nonce"]})})


server = http.server.ThreadingHTTPServer(("127.0.0.1", 8419), Provider)
threading.Thread(target=server.serve_forever, daemon=True).start()
login(BASE)  # the main admin exists, so OIDC accounts are ordinary ones


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def open(self, u):
        return self.opener.open(BASE + u if u.startswith("/") else u)

    def me(self):
        try:
            return json.loads(self.open("/api/me").read())
        except urllib.error.HTTPError as e:
            return e.code

    def sign_in(self, name, groups=(), **misbehave):
        user.update({"name": name, "groups": list(groups), "aud": CLIENT, "nonce": None, **misbehave})
        r = self.open("/api/auth/oidc/login")  # follows the redirects through the provider and back
        return urllib.parse.parse_qs(urllib.parse.urlparse(r.url).query).get("login_error", [""])[0]


assert json.loads(Client().open("/api/auth/state").read())["oidc"] == "Authelia"
name = f"oidc-{secrets.token_hex(3)}"

c = Client()
assert c.me() == 401
assert c.sign_in(name, ["friend"]) == ""
me = c.me()
assert me["username"] == name and not me["is_admin"] and not me["proxy"] and not me["has_password"], me
assert not any(k.name == "rmm_oidc" for k in c.jar), "the sign-in cookie is cleared afterwards"
assert c.me()["id"] == me["id"], "an ordinary session: stays signed in without anything from the provider"

c2 = Client()
assert c2.sign_in(name.upper(), ["friend", "admins"]) == ""
assert c2.me()["id"] == me["id"] and c2.me()["is_admin"], "same account ignoring case; the admin group makes an admin"
assert c2.sign_in(name, ["friend"]) == "" and not c2.me()["is_admin"], "and leaving it takes that away"

# The local admin's name signs in to that account, which keeps its password.
c3 = Client()
assert c3.sign_in(ADMIN, ["admins"]) == ""
assert c3.me()["main_admin"] and c3.me()["has_password"]

assert "another app" in Client().sign_in(f"{name}-y", aud="other"), "a token for another client is refused"
assert "didn't match" in Client().sign_in(f"{name}-z", nonce="forged"), "a replayed ID token is refused"
assert "username" in Client().sign_in("not ok!").lower(), "a username the app can't use is refused"

# A callback without the browser's sign-in cookie (someone else's link) is refused.
user.update(name=f"{name}-csrf", groups=[], aud=CLIENT, nonce=None)
noredirect = type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {"redirect_request": lambda *a: None})


def location(u):
    try:
        urllib.request.build_opener(noredirect).open(u)
    except urllib.error.HTTPError as e:
        return e.headers["Location"]


callback = location(location(BASE + "/api/auth/oidc/login"))  # the attacker's own, unused callback
assert "/api/auth/oidc/callback?" in callback
victim = Client()
r = victim.open(callback)
assert "login_error" in r.url and victim.me() == 401, "no session from a callback this browser didn't start"

# Local logins keep working alongside.
local = Client()
local.opener.open(urllib.request.Request(BASE + "/api/auth/login", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"username": ADMIN, "password": PASSWORD}).encode()))
assert local.me()["username"].lower() == ADMIN
print("oidc ok")
