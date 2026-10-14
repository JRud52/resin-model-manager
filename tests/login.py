"""Log the tests in: makes the admin account on a fresh server, then keeps the session cookie
for every urllib request (installed as the default opener)."""
import http.cookiejar
import json
import urllib.request

ADMIN, PASSWORD = "admin", "admin-pass"


def login(base: str, username: str = ADMIN, password: str = PASSWORD) -> str:
    """Logs in (setting up the admin first if need be) and returns the session token."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    state = json.load(opener.open(base + "/api/auth/state"))
    url = "/api/auth/setup" if state["setup"] else "/api/auth/login"
    opener.open(urllib.request.Request(base + url, data=json.dumps({"username": username, "password": password}).encode(),
                                       headers={"Content-Type": "application/json"}, method="POST"))
    urllib.request.install_opener(opener)
    return next(c.value for c in jar if c.name == "rmm_session")
