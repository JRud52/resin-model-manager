"""Accounts: each person's own models, public / private, admin mode and who may change what.

    python tests/users_test.py http://localhost:8417
(any running server; it makes the admin account if there isn't one, see login.py)
"""
import http.cookiejar
import io
import json
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import meshes as M  # noqa: E402
from login import ADMIN, PASSWORD  # noqa: E402

BASE = sys.argv[1].rstrip("/")


class Client:
    def __init__(self, username=None, password=None):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        if username:
            setup = self.get("/api/auth/state")["setup"]
            self.post("/api/auth/setup" if setup else "/api/auth/login", {"username": username, "password": password})

    def call(self, method, u, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(BASE + u, data=data, method=method, headers={"Content-Type": "application/json"})
        r = self.opener.open(req).read()
        return json.loads(r) if r[:1] in (b"{", b"[") else r

    def get(self, u):
        return self.call("GET", u)

    def post(self, u, body=None):
        return self.call("POST", u, body or {})

    def put(self, u, body):
        return self.call("PUT", u, body)

    def status_of(self, method, u, body=None):
        try:
            self.call(method, u, body)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    def upload(self, path, data, depth=0):
        q = urllib.parse.urlencode({"path": path, "size": len(data), "depth": depth})
        return self.call("PUT", f"/api/upload?{q}", raw=data)

    def models(self, q="", **params):
        return {(m["release"], m["model"]): m for m in self.get(f"/api/models?{urllib.parse.urlencode({'q': q, **params})}")["items"]
                if not m.get("mmf")}

    def wait(self):
        time.sleep(0.5)
        for _ in range(300):
            if not self.get("/api/status")["job"]["running"]:
                return
            time.sleep(0.5)
        raise SystemExit("job timed out")


assert Client().status_of("GET", "/api/models") == 401, "logged out: no models"
assert set(Client().get("/api/status")) == {"ok", "version"}, "the health check works logged out, and says little"
admin = Client(ADMIN, PASSWORD)
assert admin.get("/api/me")["is_admin"]
assert Client().status_of("POST", "/api/auth/setup", {"username": "x", "password": "123456"}) == 409, "setup only once"
assert Client().status_of("POST", "/api/auth/login", {"username": ADMIN, "password": "nope"}) == 401

def claim_removed():
    """Claims what removed accounts left (from an earlier run too), and makes it private again."""
    for r in admin.get("/api/users")["removed"]:
        admin.post("/api/models/claim", {"folder": r["folder"]})
    admin.put("/api/me/admin-mode", {"on": True})
    claimed = [m["id"] for m in admin.models(scope="mine").values() if m["public"]]
    if claimed:
        admin.post("/api/models/bulk", {"model_ids": claimed, "public": False})
    admin.put("/api/me/admin-mode", {"on": False})


users = {u["username"]: u for u in admin.get("/api/users")["users"]}
for name in ("bob", "carol"):
    if name in users:
        admin.call("DELETE", f"/api/users/{users[name]['id']}")
    admin.post("/api/users", {"username": name, "password": f"{name}-pass"})
claim_removed()
admin.post("/api/releases/creator", {"releases": ["Shared Release"], "creator": ""})  # left by an earlier run's claim
assert admin.status_of("POST", "/api/users", {"username": "user_removed", "password": "whatever"}) == 400
assert admin.status_of("POST", "/api/users", {"username": "bob", "password": "whatever"}) == 400, "names are unique"
assert admin.status_of("POST", "/api/users", {"username": "../x", "password": "whatever"}) == 400
bob, carol = Client("bob", "bob-pass"), Client("carol", "carol-pass")
assert not bob.get("/api/me")["is_admin"]
BOB = bob.get("/api/me")["folder"]  # _users/bob, or bob-2 ... when an earlier bob was removed
assert BOB.startswith("_users/bob"), BOB

with tempfile.TemporaryDirectory() as td:
    M.write_stl(Path(td) / "s.stl", M.sphere(8, 40))
    stl = (Path(td) / "s.stl").read_bytes()
# The same release and model names as the admin's: they stay separate models.
admin.upload("Shared Release/Gnome/Gnome.stl", stl)
assert bob.upload("Shared Release/Gnome/Gnome.stl", stl)["path"] == f"{BOB}/Shared Release/Gnome/Gnome.stl"
zbuf = io.BytesIO()
with zipfile.ZipFile(zbuf, "w") as z:
    z.writestr("Orc/Supported/Orc.stl", stl)
    z.writestr("Troll/Troll.stl", stl)
assert bob.upload("Orc Smiths/Bob Pack.zip", zbuf.getvalue(), depth=1)["path"] == f"{BOB}/Orc Smiths/Bob Pack.zip"
bob.post("/api/index")
bob.wait()

mine = bob.models()
assert set(mine) == {("Shared Release", "Gnome"), ("Bob Pack", "Orc"), ("Bob Pack", "Troll")}, \
    f"bob sees only his own models while everything is private: {sorted(mine)}"
assert mine[("Bob Pack", "Orc")]["creator"] == "Orc Smiths", "Creator / Release / Model in his own folder"
assert mine[("Bob Pack", "Orc")]["supported_files"] == 1
assert all(m["mine"] and m["can_edit"] and not m["public"] for m in mine.values())
assert carol.models() == {}, "carol sees none of it"
gnomes = list(admin.models(release="Shared Release").values())
assert len(gnomes) == 1 and gnomes[0]["mine"], "the admin doesn't see bob's private Gnome outside admin mode"
admin.put("/api/me/admin-mode", {"on": True})
both = admin.get("/api/models?release=Shared%20Release")["items"]
assert len(both) == 2 and len({m["id"] for m in both}) == 2, "two people's Gnomes are two models"
assert {m["owner_name"] for m in both} == {ADMIN, "bob"}
assert len(admin.get("/api/models?release=Shared%20Release&scope=mine")["items"]) == 1, "My models: only the admin's own"
admin.put("/api/me/admin-mode", {"on": False})
print("own models ok")

# Public models: everyone sees them; only the owner (or an admin) changes them.
orc, gnome, troll = (mine[("Bob Pack", "Orc")], mine[("Shared Release", "Gnome")], mine[("Bob Pack", "Troll")])
bob.put(f"/api/models/{orc['id']}/visibility", {"public": True})
seen = carol.models()
assert set(seen) == {("Bob Pack", "Orc")}, seen
assert seen[("Bob Pack", "Orc")]["owner_name"] == "bob" and not seen[("Bob Pack", "Orc")]["can_edit"]
assert carol.models(scope="mine") == {}, "My models"
detail = carol.get(f"/api/models/{orc['id']}")
assert detail["public"] and not detail["can_edit"]
assert carol.status_of("GET", f"/api/files/{detail['files'][0]['id']}/download") == 200
bob_gnome_file = bob.get(f"/api/models/{gnome['id']}")["files"][0]["id"]
assert carol.status_of("GET", f"/api/files/{bob_gnome_file}/download") == 404, "private files stay private"
assert carol.status_of("GET", f"/api/models/{gnome['id']}") == 404
assert carol.status_of("POST", f"/api/models/{orc['id']}/tags", {"add": ["mine now"]}) == 403
assert carol.status_of("PUT", f"/api/models/{orc['id']}/visibility", {"public": False}) == 403
assert carol.status_of("POST", "/api/models/bulk", {"model_ids": [orc["id"]], "add_tags": ["x"]}) == 403
assert carol.status_of("POST", "/api/overrides", {"prefixes": detail["roots"], "model": "Mine"}) == 403
assert carol.status_of("POST", "/api/overrides", {"prefixes": ["_users"], "hidden": 1}) == 403
assert carol.status_of("POST", "/api/combines", {"release": "Bob Pack", "name": "X", "model_ids": [orc["id"], troll["id"]]}) == 403
assert "Bob Pack" in {r["release"] for r in carol.get("/api/releases")}
assert "Shared Release" not in {r["release"] for r in carol.get("/api/releases")}
bob.post(f"/api/models/{orc['id']}/tags", {"add": ["painted by bob"]})
assert carol.models()[("Bob Pack", "Orc")]["tags"] == ["painted by bob"]
assert "painted by bob" in {t["tag"] for t in carol.get("/api/tags")}
assert "painted by bob" not in {t["tag"] for t in carol.get("/api/tags?scope=mine")}
# The admin can change other people's models.
admin.post(f"/api/models/{orc['id']}/tags", {"add": ["checked"]})
assert set(bob.get(f"/api/models/{orc['id']}")["tags"]) == {"painted by bob", "checked"}
# Bulk edit makes several private again; renaming keeps the choice (it is kept on the folder).
bob.post("/api/models/bulk", {"model_ids": [orc["id"], troll["id"]], "public": False})
assert carol.models() == {}
bob.post("/api/models/bulk", {"model_ids": [troll["id"]], "public": True})
bob.post("/api/overrides", {"prefixes": bob.get(f"/api/models/{troll['id']}")["roots"], "release": "Bob Pack",
                            "model": "Cave Troll", "from_model_id": troll["id"]})
assert set(carol.models()) == {("Bob Pack", "Cave Troll")}, carol.models()
print("public and private ok")

# Release settings are per person: bob's creator for Shared Release isn't the admin's.
bob.post("/api/releases/creator", {"releases": ["Shared Release"], "creator": "Gnome Works"})
assert bob.models()[("Shared Release", "Gnome")]["creator"] == "Gnome Works"
assert admin.models()[("Shared Release", "Gnome")]["creator"] == "", "the admin's Gnome keeps its creator"
# Everything at once: bob's default.
bob.put("/api/me", {"default_public": True})
assert set(carol.models()) == {("Shared Release", "Gnome"), ("Bob Pack", "Orc"), ("Bob Pack", "Cave Troll")}
bob.put("/api/me", {"default_public": False})
assert carol.models() == {}
print("release creators and defaults ok")

# MyMiniFactory: everyone syncs their own account, matched against their own models.
def sync(client, *items):
    return client.post("/api/mmf/import", {"items": [{"id": i, "name": n, "creator": "Orc Smiths", "source": "purchase"}
                                                     for i, n in items], "complete": ["purchase"]})


bob_id = bob.get("/api/me")["id"]
admin_mmf = admin.get("/api/mmf/status")
s = sync(bob, (900001, "Bob Pack"), (900002, "Dragon Hoard"))
assert s["total"] == 2 and s["missing"] == 1, "Bob Pack is in bob's library, Dragon Hoard isn't"
assert bob.get("/api/mmf/900001")["local"]["release"] == "Bob Pack"
assert admin.get("/api/mmf/status") == admin_mmf, "the admin's MyMiniFactory list is their own"
assert carol.get("/api/mmf/status")["total"] == 0
def online(client, **params):
    return {(m["owner_name"], m["id"]): m for m in client.get(f"/api/models?{urllib.parse.urlencode({'limit': 1000, **params})}")["items"]
            if m.get("mmf")}


# Like models, they are private to start with.
assert carol.status_of("GET", "/api/mmf/900002") == 404 and carol.status_of("GET", f"/api/mmf/900002?owner={bob_id}") == 404
assert not any(o == "bob" for o, _ in online(carol)), "carol doesn't see bob's private items"
bob.post("/api/mmf/900002/tags", {"add": ["bob's dragon"]})
mine_online = online(bob, source="missing")
assert list(mine_online) == [("bob", 900002)] and "bob's dragon" in mine_online[("bob", 900002)]["tags"], mine_online
assert mine_online[("bob", 900002)]["mine"] and mine_online[("bob", 900002)]["can_edit"]
admin.put("/api/me/admin-mode", {"on": True})
assert ("bob", 900002) in online(admin), "admin mode shows everyone's items"
assert ("bob", 900002) not in online(admin, scope="mine")
admin.put("/api/me/admin-mode", {"on": False})
assert ("bob", 900002) not in online(admin)
# Public: everyone sees them, read-only, with whose they are.
bob.put("/api/mmf/900002/visibility", {"public": True})
dragon = online(carol)[("bob", 900002)]
assert dragon["public"] and not dragon["mine"] and not dragon["can_edit"], dragon
assert ("bob", 900002) not in online(carol, scope="mine")
assert carol.get(f"/api/mmf/900002?owner={bob_id}")["owner_name"] == "bob"
assert carol.status_of("POST", f"/api/mmf/900002/tags?owner={bob_id}", {"add": ["x"]}) == 403
assert carol.status_of("PUT", f"/api/mmf/900002/visibility?owner={bob_id}", {"public": False}) == 403
assert carol.status_of("POST", "/api/models/bulk", {"mmf_refs": [f"{bob_id}:900002"], "add_tags": ["x"]}) == 403
assert "Orc Smiths" in {c["creator"] for c in carol.get("/api/creators") if c["mmf"]}
admin.post(f"/api/mmf/900002/tags?owner={bob_id}", {"add": ["checked"]})
assert "checked" in bob.get("/api/mmf/900002")["tags"], "admins can change them"
# The same item in two people's libraries: listed once for each viewer, the viewer's own first,
# each with its owner's tags.
assert sync(carol, (900002, "Dragon Hoard"))["missing"] == 1
assert "bob's dragon" not in carol.get("/api/mmf/900002")["tags"], "bob's tags stay bob's"
assert [k for k in online(carol) if k[1] == 900002] == [("carol", 900002)]
carol.post("/api/models/bulk", {"mmf_ids": [900002], "add_tags": ["carol's"]})
assert "carol's" not in bob.get("/api/mmf/900002")["tags"] and "carol's" in carol.get("/api/mmf/900002")["tags"]
bob.post("/api/models/bulk", {"mmf_refs": [f"{bob_id}:900001", f"{bob_id}:900002"], "public": False})
assert not any(o == "bob" for o, _ in online(carol))
sync(carol)
assert carol.get("/api/mmf/status")["total"] == 0 and bob.get("/api/mmf/status")["total"] == 2, "carol's sync leaves bob's list alone"
assert carol.status_of("GET", "/api/mmf/900002") == 404
# "Your models are public" covers the items too.
bob.put("/api/me", {"default_public": True})
assert {("bob", 900001), ("bob", 900002)} <= set(online(carol, source="mmf"))
bob.put("/api/me", {"default_public": False})
assert not any(o == "bob" for o, _ in online(carol, source="mmf"))
print("MyMiniFactory per person ok")

# Admin only: settings, users, folder mappings, creator renames.
for method, u, body in (("PUT", "/api/settings", {"preview_size": 256}),
                        ("GET", "/api/users", None), ("GET", "/api/maps", None), ("POST", "/api/import", None),
                        ("POST", "/api/creators/rename", {"creator": "Orc Smiths", "name": "X"}),
                        ("PUT", "/api/me/admin-mode", {"on": True})):
    assert bob.status_of(method, u, body) == 403, u
assert bob.get("/api/settings")["preferred_format"], "everyone reads the display preferences"
assert all(o["prefix"].startswith(BOB + "/") for o in bob.get("/api/overrides"))
# Passwords
assert bob.status_of("PUT", "/api/me", {"password": "new-pass", "current_password": "wrong"}) == 400
bob.put("/api/me", {"password": "bob-pass2", "current_password": "bob-pass"})
assert bob.get("/api/me")["username"] == "bob", "the session that changed it stays logged in"
assert Client().status_of("POST", "/api/auth/login", {"username": "bob", "password": "bob-pass"}) == 401
bob = Client("bob", "bob-pass2")
print("admin only and passwords ok")

# Removing an account: the models stay as user_removed's, public or private as they were, with their tags.
bob_id = next(u["id"] for u in admin.get("/api/users")["users"] if u["username"] == "bob")
bob.post("/api/models/bulk", {"model_ids": [bob.models()[("Bob Pack", "Cave Troll")]["id"]], "public": True})
admin.call("DELETE", f"/api/users/{bob_id}")
assert bob.status_of("GET", "/api/models") == 401, "logged out"
assert set(carol.models()) == {("Bob Pack", "Cave Troll")}, "a public model stays public"
cave = carol.models()[("Bob Pack", "Cave Troll")]
assert cave["owner_name"] == "user_removed" and not cave["can_edit"]
assert carol.status_of("POST", "/api/models/claim", {"model_ids": [cave["id"]]}) == 403, "only admins claim"
assert {(m["release"], m["model"]) for m in admin.get("/api/models?source=local&limit=1000")["items"]
        if m["owner"] == 0} == {("Bob Pack", "Cave Troll")}, "the rest is private"
admin.put("/api/me/admin-mode", {"on": True})
left = {(m["release"], m["model"]): m for m in admin.get("/api/models?source=local&limit=1000")["items"] if m["owner"] == 0}
assert set(left) == {("Bob Pack", "Orc"), ("Bob Pack", "Cave Troll"), ("Shared Release", "Gnome")}, left
assert set(left[("Bob Pack", "Orc")]["tags"]) == {"painted by bob", "checked"}, "tags kept"
assert left[("Shared Release", "Gnome")]["creator"] == "Gnome Works", "release creator kept"
assert all(m["can_edit"] and not m["mine"] for m in left.values()), "admins can still change them"
removed = admin.get("/api/users")["removed"]
assert {"folder": BOB, "models": 3} in removed, removed
# A new account with the same name starts with a folder of its own.
admin.post("/api/users", {"username": "bob", "password": "bob-pass"})
new_bob = Client("bob", "bob-pass")
assert new_bob.models(scope="mine") == {}
assert new_bob.get("/api/mmf/status")["total"] == 0, "nor the old bob's MyMiniFactory list"
# Claiming: one model, then everything left.
admin.post("/api/models/claim", {"model_ids": [left[("Bob Pack", "Orc")]["id"]]})
orc = admin.models(scope="mine")[("Bob Pack", "Orc")]
assert {"painted by bob", "checked"} <= set(orc["tags"]) and orc["mine"], orc
assert {(m["release"], m["model"]) for m in admin.get("/api/models?source=local&limit=1000")["items"]
        if m["owner"] == 0} == {("Bob Pack", "Cave Troll"), ("Shared Release", "Gnome")}
admin.post("/api/models/claim", {"folder": BOB})
mine_now = admin.models(scope="mine")
assert {("Bob Pack", "Cave Troll"), ("Shared Release", "Gnome")} <= set(mine_now)
assert mine_now[("Shared Release", "Gnome")]["creator"] == "Gnome Works"
assert BOB not in {r["folder"] for r in admin.get("/api/users")["removed"]}
admin.put("/api/me/admin-mode", {"on": False})
claim_removed()
carol.post("/api/auth/logout")
assert carol.status_of("GET", "/api/models") == 401
print("USERS OK")
