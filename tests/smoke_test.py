"""End-to-end check against a running server that points at a sample library.

    python tests/make_sample_library.py /tmp/rmm/src
    (start the app with SOURCE_DIR=/tmp/rmm/src)
    python tests/smoke_test.py http://localhost:8417 /tmp/rmm/src
"""
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = sys.argv[1].rstrip("/")
SRC = Path(sys.argv[2])


def get(u):
    return json.load(urllib.request.urlopen(BASE + u))


def post(u, body=None):
    req = urllib.request.Request(BASE + u, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req))


def snapshot():
    return {str(p.relative_to(SRC)): (p.stat().st_mtime, hashlib.md5(p.read_bytes()).hexdigest())
            for p in SRC.rglob("*") if p.is_file()}


def wait_job():
    for _ in range(300):
        s = get("/api/status")
        if not s["job"]["running"]:
            return s
        time.sleep(0.5)
    raise SystemExit("job timed out")


before = snapshot()
post("/api/import")
time.sleep(0.5)
s = wait_job()
assert not s["job"]["last"]["error"], s["job"]["last"]
models = {m["model"]: m for m in get("/api/models")["items"]}
print("models:", sorted(models))
expected = {"Red Dragon", "Knight", "Goblin Boss", "Rat Captain", "Rat Trooper", "Lich King", "Skeleton",
            "Orc Warboss", "Banner"}
assert expected <= set(models), expected - set(models)
assert models["Red Dragon"]["supported_files"] == 2 and models["Red Dragon"]["unsupported_files"] == 2
assert models["Lich King"]["supported_files"] == 1, "7z support detection"
assert models["Goblin Boss"]["options"] == 1, "option folders"

# Previews, including members of .zip and .7z archives.
for name in ("Rat Captain", "Lich King", "Red Dragon"):
    r = urllib.request.urlopen(f"{BASE}/api/files/{models[name]['cover']}/preview")
    assert r.headers["Content-Type"] == "image/webp" and len(r.read()) > 1000, name
print("previews ok")

# Bundled preview pictures: matched to models or releases, and shown before rendered STLs.
def img_names(model):
    return [i["name"] for i in get(f"/api/models/{models[model]['id']}")["images"]]
assert img_names("Red Dragon") == ["DL_RedDragon_Promo.jpg", "Red_Dragon_render.png"], "promo picture first"
assert sorted(img_names("Knight")) == ["01.jpg", "Dragon Lords - Knight.jpg", "Knight_front.jpg"], img_names("Knight")
assert img_names("Rat Trooper") == ["02_rat_trooper_painted.png"]
assert "orc.jpg" in img_names("Orc Warboss")
assert img_names("Rat Captain") == ["rat_captain_preview.jpg"]
assert img_names("Lich King") == ["lich_king.webp"]
assert "Orc_Warboss.jpg" in img_names("Orc Warboss")
assert img_names("Goblin Boss") == ["goblin_banner.png"], "single-model release uses the release picture"
assert img_names("Skeleton") == [] and models["Skeleton"]["cover_image"] is None
for name in ("Red Dragon", "Rat Captain", "Lich King", "Goblin Boss"):
    assert models[name]["cover_image"], name
    r = urllib.request.urlopen(f"{BASE}/api/images/{models[name]['cover_image']}/preview")
    assert r.headers["Content-Type"] == "image/webp" and len(r.read()) > 100, name
rel = [i["name"] for i in get("/api/releases/images?release=Dragon%20Lords")]
assert rel == ["Dragon Lords Cover.jpg"], rel
assert [i["name"] for i in get("/api/releases/images?release=Necro%20Lords")] == ["Necro Lords Promo.png"]
full = urllib.request.urlopen(f"{BASE}/api/images/{models['Lich King']['cover_image']}/full").read()
assert full[:4] == b"RIFF", "full picture streamed from the 7z"
# Moving a picture by hand: onto a model, then back to the whole release.
cover = "Dragon Lords/Dragon Lords Cover.jpg"
post("/api/overrides", {"prefixes": [cover], "release": "Dragon Lords", "model": "Knight"})
assert "Dragon Lords Cover.jpg" in img_names("Knight")
assert get("/api/releases/images?release=Dragon%20Lords") == []
post("/api/overrides", {"prefixes": ["Dragon Lords/Images/Knight/01.jpg"], "release": "Dragon Lords", "model": ""})
assert "01.jpg" not in img_names("Knight")
for o in get("/api/overrides"):
    if o["prefix"] in (cover, "Dragon Lords/Images/Knight/01.jpg"):
        urllib.request.urlopen(urllib.request.Request(f"{BASE}/api/overrides/{o['id']}", method="DELETE"))
assert [i["name"] for i in get("/api/releases/images?release=Dragon%20Lords")] == ["Dragon Lords Cover.jpg"]
print("bundled pictures ok")

# Downloads straight out of archives.
for name in ("Rat Captain", "Lich King"):
    d = get(f"/api/models/{models[name]['id']}")
    for f in d["files"]:
        data = urllib.request.urlopen(f"{BASE}/api/files/{f['id']}/download").read()
        assert len(data) == f["size"], f
print("downloads ok")

# Corrections: merge Banner into Orc Warboss.
d = get(f"/api/models/{models['Banner']['id']}")
post("/api/overrides", {"prefixes": d["roots"], "model": "Orc Warboss", "release": "Bits Pack"})
models = {m["model"]: m for m in get("/api/models")["items"]}
assert "Banner" not in models and models["Orc Warboss"]["files"] == 4
print("corrections ok")

# Tags: one model, a whole release, filtering and search, and tags following a rename.
rc = models["Rat Captain"]["id"]
assert post(f"/api/models/{rc}/tags", {"add": ["Sci-fi", "painted"]})["tags"] == ["painted", "Sci-fi"]
assert post("/api/releases/tags", {"release": "Dragon Lords", "add": ["fantasy", " Dragons "]})["models"] == 2
tags = {t["tag"]: t["models"] for t in get("/api/tags")}
assert tags == {"Dragons": 2, "fantasy": 2, "painted": 1, "Sci-fi": 1}, tags
names = lambda u: sorted(m["model"] for m in get(u)["items"])
assert names("/api/models?tags=fantasy") == ["Knight", "Red Dragon"]
assert names("/api/models?tags=fantasy,painted") == []
assert names("/api/models?q=sci-fi") == ["Rat Captain"]
assert names("/api/models?q=tag:painted") == ["Rat Captain"]
post("/api/releases/tags", {"release": "Dragon Lords", "remove": ["dragons"]})
assert "Dragons" not in {t["tag"] for t in get("/api/tags")}
d = get(f"/api/models/{rc}")
post("/api/overrides", {"prefixes": d["roots"], "model": "Rat Commander", "release": "Space Rats", "from_model_id": rc})
assert names("/api/models?tags=painted") == ["Rat Commander"]
rcm = next(m for m in get("/api/models")["items"] if m["model"] == "Rat Commander")
assert rcm["cover_image"], "pictures follow a renamed model"
print("tags ok")

# Creators: set on releases in bulk, filter and search by them, clear one.
assert post("/api/releases/creator", {"releases": ["Dragon Lords", "Goblin Warband"], "creator": " Mini  Forge "})["creator"] == "Mini Forge"
assert post("/api/releases/creator", {"releases": ["Bits Pack"], "creator": "mini forge"})["creator"] == "Mini Forge"
creators = {c["creator"]: c["releases"] for c in get("/api/creators")}
assert creators.get("Mini Forge") == 3 and "" in creators, creators
assert names("/api/models?creator=Mini%20Forge") == ["Goblin Boss", "Knight", "Orc Warboss", "Red Dragon"]
assert "Rat Commander" in names("/api/models?creator=")
assert names("/api/models?q=forge&release=Dragon%20Lords") == ["Knight", "Red Dragon"]
assert {r["release"]: r["creator"] for r in get("/api/releases?creator=Mini%20Forge")} == \
    {"Dragon Lords": "Mini Forge", "Goblin Warband": "Mini Forge", "Bits Pack": "Mini Forge"}
post("/api/releases/creator", {"releases": ["Bits Pack"], "creator": ""})
assert {c["creator"]: c["releases"] for c in get("/api/creators")}["Mini Forge"] == 2
print("creators ok")

# Re-import copies nothing new, and the source library is unchanged.
post("/api/import")
time.sleep(0.5)
s = wait_job()
assert "Imported 0 files" in s["job"]["last"]["message"], s["job"]["last"]
assert snapshot() == before, "source library changed!"
print("source untouched, re-import skipped existing files")
assert get(f"/api/models/{models['Knight']['id']}")["creator"] == "Mini Forge", "creator lost on re-index"

# Uploads from the browser: a loose file into a folder, a release archive, duplicates and bad paths.
import io, urllib.error, urllib.parse, zipfile
sys.path.insert(0, str(Path(__file__).parent))
import meshes as M


def put(path, data, depth=0):
    q = urllib.parse.urlencode({"path": path, "size": len(data), "depth": depth})
    req = urllib.request.Request(f"{BASE}/api/upload?{q}", data=data, method="PUT")
    return json.load(urllib.request.urlopen(req))


import tempfile
with tempfile.TemporaryDirectory() as td:
    M.write_stl(Path(td) / "s.stl", M.sphere(8, 40))
    stl = (Path(td) / "s.stl").read_bytes()
zbuf = io.BytesIO()
with zipfile.ZipFile(zbuf, "w") as z:
    z.writestr("Hover Tank/Supported/Hover_Tank.stl", stl)
    z.writestr("Hover Tank/Unsupported/Hover_Tank.stl", stl)
assert put("Uploaded Release/Gnome/Gnome.stl", stl)["status"] == "saved"
assert put("Tank Pack.zip", zbuf.getvalue())["status"] == "saved"
assert put("Uploaded Release/Gnome/Gnome.stl", stl)["status"] == "exists"
assert put("Uploaded Release/Gnome/Gnome.stl", stl + b"\0")["path"] == "Uploaded Release/Gnome/Gnome (2).stl"
from PIL import Image
pbuf = io.BytesIO(); Image.new("RGB", (300, 200), "teal").save(pbuf, "PNG")
assert put("Uploaded Release/Gnome/gnome.png", pbuf.getvalue())["status"] == "saved"
for bad in ("../escape.stl", "/abs/../../x.stl", "notes.txt", ".hidden/x.stl"):
    try:
        put(bad, stl)
        raise SystemExit(f"upload of {bad!r} was accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 400, (bad, e.code)
post("/api/overrides", {"prefixes": ["Tank Pack"], "creator": "Tank Works"})  # the Import dialog's Creator field
post("/api/index")
time.sleep(0.5)
wait_job()
models = {(m["release"], m["model"]): m for m in get("/api/models")["items"]}
assert models[("Tank Pack", "Hover Tank")]["creator"] == "Tank Works"
assert models[("Uploaded Release", "Gnome")]["creator"] == ""
assert ("Uploaded Release", "Gnome") in models, sorted(models)
assert models[("Uploaded Release", "Gnome")]["cover_image"], "uploaded picture used as cover"
tank = models[("Tank Pack", "Hover Tank")]
assert tank["supported_files"] == 1 and tank["unsupported_files"] == 1, tank
print("uploads ok")

# Layouts per folder: the copied NAS library follows the setting, browser uploads stay Release / Model,
# and an upload with a creator is filed as Creator / Release / Model.
mbuf = io.BytesIO()
with zipfile.ZipFile(mbuf, "w") as z:
    z.writestr("Walker/Walker.stl", stl)
    z.writestr("Strider/Strider_sup.stl", stl)
assert put("Tank Works/Mech Pack.zip", mbuf.getvalue(), depth=1)["path"] == "Tank Works/Mech Pack.zip"
req = urllib.request.Request(f"{BASE}/api/settings", data=json.dumps({"release_depth": 1}).encode(),
                             headers={"Content-Type": "application/json"}, method="PUT")
urllib.request.urlopen(req)
time.sleep(0.5)
wait_job()
models = {(m["release"], m["model"]): m for m in get("/api/models")["items"]}
assert models[("Mech Pack", "Walker")]["creator"] == "Tank Works", sorted(models)
assert ("Mech Pack", "Strider") in models
assert ("Uploaded Release", "Gnome") in models and ("Tank Pack", "Hover Tank") in models, "uploads regrouped"
assert models[("Supported", "Red Dragon")]["creator"] == "Dragon Lords", "copied library follows the setting"
req = urllib.request.Request(f"{BASE}/api/settings", data=json.dumps({"release_depth": 0}).encode(),
                             headers={"Content-Type": "application/json"}, method="PUT")
urllib.request.urlopen(req)
time.sleep(0.5)
wait_job()
models = {(m["release"], m["model"]): m for m in get("/api/models")["items"]}
assert models[("Mech Pack", "Walker")]["creator"] == "Tank Works" and ("Dragon Lords", "Red Dragon") in models
print("layouts ok")

# Preferred format / version: text settings, defaulting to STL + supported.
def put_settings(body):
    req = urllib.request.Request(f"{BASE}/api/settings", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="PUT")
    return json.load(urllib.request.urlopen(req))
s = get("/api/settings")
assert s["preferred_format"] == "stl" and s["preferred_support"] == "supported", s
s = put_settings({"preferred_format": "lys", "preferred_support": ""})
assert s["preferred_format"] == "lys" and s["preferred_support"] == "" and s["release_depth"] == 0, s
try:
    put_settings({"preferred_format": "exe"})
    raise SystemExit("bad format accepted")
except urllib.error.HTTPError as e:
    assert e.code == 400
put_settings({"preferred_format": "stl", "preferred_support": "supported"})
print("preferences ok")

for _ in range(120):
    pv = get("/api/status")["previews"]
    if not pv.get("pending"):
        break
    time.sleep(1)
print("preview states:", pv)
print("ALL OK")
