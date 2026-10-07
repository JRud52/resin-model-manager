"""MyMiniFactory sync check: runs the bookmarklet on a fake myminifactory.com in Chromium.

Needs a running app on the sample library (see smoke_test.py) and Playwright:
    python tests/mmf_test.py http://localhost:8417 [screenshot-dir]
"""
import json
import os
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
SHOTS = sys.argv[2] if len(sys.argv) > 2 else None
CDN = "https://cdn.example.com"


def obj(i, name, creator="Dragon Forge", **extra):
    return {"id": i, "name": name, "user_name": creator, "absolute_url": f"/object/3d-print-{i}",
            "images": {"items": [{"is_primary": True, "standard": {"url": f"{CDN}/{i}.png"}}]}, **extra}


WYRM_PICS = {"items": [{"is_primary": True, "standard": {"url": f"{CDN}/2.png"}}, {"large": {"url": f"{CDN}/7.png"}},
                       {"standard": {"url": f"{CDN}/8.png"}}]}
PURCHASES = [obj(1, "Lich King - Supported"), obj(2, "Ancient Wyrm", images=WYRM_PICS), obj(3, "Javascript", absolute_url="javascript:alert(1)")]
PLEDGES = [obj(4, "Siege Tower", creator="Castle_Works!", pledges={"items": [{"name": "Kickstarter: Castle Siege"}]})]
# "Mini Forge" here. The shaman's display name is on its designer, its user_name is the account slug.
def small(name):  # the library's own small picture link
    return {"items": [{"is_primary": True, "standard": {"url": f"{CDN}/object-images/gb/images/230X230-{name}.png"}}]}


TRIBE = [obj(5, "Goblin Boss", creator="MiniForge", images=small("boss")),
         obj(6, "Goblin Shaman", creator="mini-forge-1984", images=small("shaman"), designer={"name": "MiniForge", "username": "mini-forge-1984"})]

def png(i):
    from io import BytesIO
    from PIL import Image
    b = BytesIO()
    Image.new("RGB", (64, 64), ((i * 70) % 256, 120, 200 - i * 25)).save(b, "PNG")
    return b.getvalue()


def page_of(items, page, size=2):
    return {"total_count": len(items), "items": items[(page - 1) * size:page * size]}


# The library as the site reads it since 2026: one list, release names from metadata lists,
# details from /api/data-library/objects. Switched on for the last part of the test.
NEW_API = []
TRIBE_RELEASE = "type:tribes-tier;owner:77;tribe:9;tier:1;yearmonth:202309"
PREVIEWS = [
    {"originalId": 1, "id": "object-1", "type": "object", "name": "Lich King - Supported", "source": "PURCHASE",
     "creatorName": "Dragon Forge"},
    {"originalId": 3, "id": "bundle-3", "type": "bundle", "name": "Some Bundle", "source": "PURCHASE"},
    {"originalId": 5, "id": "object-5", "type": "object", "name": "Goblin Boss", "source": "TRIBE",
     "release": TRIBE_RELEASE, "creatorName": "MiniForge", "creatorId": 9, "tags": ["Goblin", {"name": "Fantasy"}, " goblin "]},
    {"originalId": 14, "id": "object-14", "type": "object", "name": "Tadpole Hero", "source": "TRIBE",
     "release": "type:campaign-tier;orderId:5;tierId:901", "creatorName": "Frog Folk"},
    {"originalId": 15, "id": "object-15", "type": "object", "name": "Shroud Arm", "source": "PURCHASE",
     "release": "type:store-bundle;orderId:6;bundleId:1123", "creatorName": "Fleshcraft"},
    {"originalId": 11, "id": "object-11", "type": "object", "name": "Orc Warlord", "source": "USER_GROUP",
     "release": "32391", "creatorName": "Orc Works", "tags": "orc, Warhammer"},
    {"originalId": 12, "id": "object-12", "type": "object", "name": "Plus Paladin", "source": "MMFPLUS", "release": "39939"},
    {"originalId": 13, "id": "object-13", "type": "object", "name": "Free Frog", "source": "DOWNLOAD"},
    {"originalId": 13, "id": "object-13", "type": "object", "name": "Free Frog", "source": "FRONTIER",
     "campaignId": 555, "release": "901"},
]
NEW_META = {
    "tribes_metadata": [{"id": 9, "name": "Greenskin Tribe"}],
    "tribe_releases_metadata/9": [{"id": TRIBE_RELEASE, "label": "09/2023 | tier: Elders"}, {"id": "w", "label": "Welcome Pack"}],
    "userGroups_metadata": [{"id": 4, "name": "Orc Works"}],
    "userGroup_releases_metadata/4": [{"id": 32391, "label": "38. OPR April 2023 Rewards"}],
    "mmfplus_releases_metadata": [{"id": 39939, "label": "September 2023 MMF+ Release"}],
    "frontiers_metadata": [{"id": 555, "name": "Frog Kingdom"}],
    "frontier_releases_metadata/555": {"pledges": [{"id": 901, "name": "Tadpole"}], "addons": []},
    "bundles_metadata": [{"id": "bundle-1123", "originalId": 1123, "name": "Shroudborne"}],
}


def new_api(url):
    from urllib.parse import parse_qs, unquote, urlparse
    u = urlparse(url)
    path = unquote(u.path)[len("/api/data-library/"):]
    if path == "objectPreviews":
        return PREVIEWS
    if path == "objects":
        ids = parse_qs(u.query).get("ids[]", [])
        return [{"originalId": int(i), "name": next(p["name"] for p in PREVIEWS if str(p["originalId"]) == i),
                 "url": f"thing-{i}", "creator": {"username": f"maker-{i}", "name": "" if i == "12" else None},
                 "previewUrl": f"{CDN}/object-images/p{i}/images/1000X1000-p.png",
                 "images": [{"url": f"{CDN}/object-images/p{i}/images/720X720-p.png",
                             "thumbnailUrl": f"{CDN}/object-images/p{i}/images/230X230-p.png"}]} for i in ids]
    return NEW_META.get(path)


def fake_mmf(route):
    url = route.request.url
    page = int(url.split("page=")[1]) if "page=" in url else 1
    if NEW_API and "/api/data-library/" in url:
        body = new_api(url)
        if body is None:
            return route.fulfill(status=404, body="{}")
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    if "/data-library/purchases" in url:
        body = page_of(PURCHASES, page)
    elif "/data-library/campaigns" in url:
        body = page_of(PLEDGES, page)
    elif "/data-library/tribes" in url:
        body = page_of([{"id": 9, "name": "Greenskin Tribe",
                         "groups": {"items": [{"id": "all/9", "name": "All"}, {"id": "42", "name": "March"}]}}], page)
    elif "/data-library/group/42" in url:
        body = page_of(TRIBE, page)
    elif url.endswith("/api/v2/objects/2"):  # MyMiniFactory's own data for the item: just the listing's images
        a = f"{CDN}/object-images/aa2/images"
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": 2, "images": [
            {"is_primary": n == "a", "thumbnail": {"url": f"{a}/230X230-wyrm-{n}.png"},
             "standard": {"url": f"{a}/720X720-wyrm-{n}.png"}, "large": {"url": f"{a}/1000X1000-wyrm-{n}.png"}}
            for n in "abcd"]}))
    elif "/api/v2/objects/" in url:
        return route.fulfill(status=404, body="{}")
    elif "/data-library/" in url:
        return route.fulfill(status=404, body="{}")
    else:
        return route.fulfill(status=200, content_type="text/html", body="<html><body><h1>Library</h1></body></html>")
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def get(u):
    return json.load(urllib.request.urlopen(BASE + u))


def post(u, b=None):
    return json.load(urllib.request.urlopen(urllib.request.Request(
        BASE + u, data=json.dumps(b or {}).encode(), headers={"Content-Type": "application/json"}, method="POST")))


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
    ctx.route("https://www.myminifactory.com/**", fake_mmf)
    ctx.route(f"{CDN}/**", lambda r: r.fulfill(status=404, body="") if "720X720-shaman" in r.request.url else r.fulfill(
        status=200, content_type="image/png", body=png(sum(map(ord, r.request.url.rsplit("/", 1)[1])) % 7)))
    app = ctx.new_page()
    app.goto(BASE)
    app.click("#mmfSyncBtn")
    app.wait_for_selector("#mmfDlg[open]")
    href = app.get_attribute("#mmfBookmarklet", "href")
    assert href.startswith("javascript:"), href
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-dialog.png")
    app.keyboard.press("Escape")

    mmf = ctx.new_page()
    mmf.goto("https://www.myminifactory.com/library")
    with ctx.expect_page() as popup_info:
        mmf.evaluate(urllib.request.unquote(href[len("javascript:"):]))
    popup = popup_info.value
    popup.wait_for_function("document.getElementById('msg').textContent.startsWith('Synced')", timeout=20000)
    print("sync page:", popup.text_content("#msg"))
    mmf.wait_for_function("document.body.innerText.includes('Resin Models: Synced')", timeout=5000)

    s = get("/api/mmf/status")
    print("status:", s)
    assert s["total"] == 6 and s["sources"] == {"purchase": 3, "pledge": 1, "tribe": 2}, s
    items = {m["name"]: m for m in get("/api/mmf")["items"]}
    assert items["Lich King - Supported"]["local"]["model_id"], "matches the local Lich King model"
    assert items["Goblin Boss"]["local"], "matches the local Goblin Boss model"
    assert not items["Ancient Wyrm"]["local"]
    assert items["Siege Tower"]["sources"] == [{"source": "pledge", "collection": "Kickstarter: Castle Siege"}]
    assert items["Goblin Shaman"]["sources"][0]["collection"] == "Greenskin Tribe"
    assert items["Javascript"]["url"] == "https://www.myminifactory.com/object/3", "unsafe link dropped"
    assert items["Ancient Wyrm"]["url"] == "https://www.myminifactory.com/object/3d-print-2"
    assert s["missing"] == 4, s

    # MyMiniFactory creators join the creator filter, merged with local ones of nearly the same name.
    creators = {c["creator"]: c for c in get("/api/creators")}
    assert creators["Mini Forge"]["mmf"] == 2 and creators["Mini Forge"]["releases"], creators
    assert "MiniForge" not in creators, creators
    assert creators["Castle_Works!"]["releases"] == 0 and creators["Castle_Works!"]["mmf"] == 1, creators
    assert get("/api/mmf?creator=castle%20works")["total"] == 1
    # Releases not in the library are listed with the local ones: a pledge or tribe is one release.
    online = {r["release"]: r for r in get("/api/releases") if r["mmf"]}
    assert set(online) == {"Ancient Wyrm", "Kickstarter: Castle Siege", "Javascript", "Greenskin Tribe"}, online
    assert online["Greenskin Tribe"]["creator"] == "MiniForge" and online["Greenskin Tribe"]["models"] == 0
    assert [i["id"] for i in get("/api/mmf?release=greenskin%20tribe")["items"]] == [6], "matched Goblin Boss left out"
    # The grid mixes in the items not in the library like models; the source filter picks either side.
    local_total = get("/api/models?source=local")["total"]
    grid = get("/api/models?limit=1000")
    assert grid["total"] == local_total + 4 and sorted(m["id"] for m in grid["items"] if m.get("mmf")) == [2, 3, 4, 6], grid["total"]
    keys = [(m.get("grid_release") or m["release"]).casefold() for m in grid["items"]]
    assert keys == sorted(keys), "sorted by release with the local models"
    assert [m["id"] for m in get("/api/models?limit=2&offset=0")["items"]] + [m["id"] for m in get("/api/models?limit=1000&offset=2")["items"]] == \
        [m["id"] for m in grid["items"]], "paging"
    assert [m["id"] for m in get("/api/models?release=Greenskin%20Tribe")["items"]] == [6]
    assert get("/api/models?source=missing")["total"] == 4 and get("/api/models?source=mmf")["total"] == 6
    assert not any(m.get("mmf") for m in get("/api/models?source=local&limit=1000")["items"])
    assert {m["id"] for m in get("/api/models?q=goblin")["items"] if m.get("mmf")} == {6}
    assert not any(m.get("mmf") for m in get("/api/models?tags=fantasy")["items"]), "only library models have tags"
    assert all(r["models"] for r in get("/api/releases?source=local"))
    assert {r["release"] for r in get("/api/releases?source=missing")} == set(online)

    # Renaming a creator covers its releases and MyMiniFactory items, and survives re-indexing.
    assert post("/api/creators/rename", {"creator": "Mini Forge", "name": "The Mini Forge Co"})["creator"] == "The Mini Forge Co"
    post("/api/index")
    time.sleep(0.5)
    while get("/api/status")["job"]["running"]:
        time.sleep(0.3)
    renamed = {c["creator"]: c for c in get("/api/creators")}
    assert "Mini Forge" not in renamed and renamed["The Mini Forge Co"]["mmf"] == 2, renamed
    assert renamed["The Mini Forge Co"]["releases"] == creators["Mini Forge"]["releases"], renamed
    assert get("/api/models?creator=The%20Mini%20Forge%20Co")["total"] > 0
    assert get("/api/mmf?creator=The%20Mini%20Forge%20Co")["total"] == 2
    assert {m["creator"] for m in get("/api/mmf?creator=The%20Mini%20Forge%20Co")["items"]} == {"The Mini Forge Co"}
    post("/api/creators/rename", {"creator": "The Mini Forge Co", "name": "Forge Minis"})  # renaming again
    assert get("/api/mmf?creator=Forge%20Minis")["total"] == 2 and get("/api/models?creator=Forge%20Minis")["total"] > 0
    post("/api/creators/rename", {"creator": "Castle_Works!", "name": "Castle Works"})  # only on MyMiniFactory
    assert get("/api/mmf/4")["creator"] == "Castle Works"
    post("/api/creators/rename", {"creator": "Forge Minis", "name": ""})  # undo
    post("/api/creators/rename", {"creator": "Castle Works", "name": ""})
    assert {c["creator"]: c for c in get("/api/creators")}.keys() == creators.keys()
    app.reload()
    app.fill(".side-filter input[data-for='releases']", "castle siege")  # long lists show the first few
    app.click("#releases li[data-r='Kickstarter: Castle Siege']")
    app.wait_for_function("document.getElementById('crumbs').textContent.includes('Castle Siege')")
    assert app.locator("#grid .card").count() == 1 and app.locator("#grid .card.mmf[data-mmf='4']").count() == 1
    app.click(".side-title[data-sec='releases']")  # sections fold, and stay folded after a reload
    assert app.locator("#releases").is_hidden()
    app.reload()
    assert app.locator("#releases").is_hidden()
    assert "Castle Siege" not in app.locator(".side-title[data-sec='releases']").inner_text(), "selection reset on reload"
    app.click(".side-title[data-sec='releases']")
    # Long lists show their first 8 entries and "Show all"; the filter box finds the rest.
    app.wait_for_function("document.querySelectorAll(\"#releases > li[data-r]:not(.cut):not([data-r=''])\").length === 8")
    rows = app.locator("#releases > li[data-r]:not([data-r=''])").count()
    assert rows > 8 and app.locator("#releases > li[data-r]:not(.cut):not([data-r=''])").count() == 8
    assert app.locator("#releases li[data-r='Kickstarter: Castle Siege']").is_hidden()
    app.click("#releases .show-more")
    app.wait_for_selector("#releases li[data-r='Kickstarter: Castle Siege']")
    app.reload()  # "Show all" lasts until the next reload
    app.wait_for_function("document.querySelectorAll(\"#releases > li[data-r]:not(.cut):not([data-r=''])\").length === 8")
    app.click("#releases .show-more")
    app.wait_for_selector("#releases li[data-r='Kickstarter: Castle Siege']")
    app.click("#releases .show-more")  # "Show fewer"
    app.fill(".side-filter input[data-for='releases']", "kickstarter")
    assert app.locator("#releases > li[data-r]:not(.cut):not([data-r=''])").count() == 1
    app.fill(".side-filter input[data-for='releases']", "")
    app.click("#creators li[data-c='Castle_Works!']")  # only on MyMiniFactory: its item is in the grid
    app.wait_for_function("(t) => document.getElementById('crumbs').textContent.includes(t)", arg='Castle_Works!')
    app.wait_for_selector("#grid .card.mmf[data-mmf='4']")
    assert app.locator("#grid .card").count() == 1
    app.click("#creators li[data-c='Mini Forge']")  # local models and the item not in the library
    app.wait_for_function("(t) => document.getElementById('crumbs').textContent.includes(t)", arg='Mini Forge /')
    app.wait_for_selector("#grid .card.mmf[data-mmf='6']")
    assert app.locator("#grid .card.mmf").count() == 1 and app.locator("#grid .card:not(.mmf)").count() >= 1
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-creator.png")
    app.click("#mmfList li[data-src='mmf']")  # only MyMiniFactory, the ones in the library too
    app.wait_for_function("(t) => document.getElementById('crumbs').textContent.includes(t)", arg='MyMiniFactory / Mini Forge')
    app.wait_for_selector("#grid .card.mmf[data-mmf='5']")
    assert app.locator("#grid .card.mmf").count() == 2 and app.locator("#grid .card:not(.mmf)").count() == 0
    # The model window asks for the 720px version of a small picture, and keeps the small one without it.
    for oid, want in (("5", "/720X720-boss.png"), ("6", "/230X230-shaman.png")):
        app.click(f"#grid .card.mmf[data-mmf='{oid}']")
        app.wait_for_selector("#modelDlg.mmf-mode[open]")
        app.wait_for_function("(w) => document.getElementById('mPreview').src.endsWith(w) && "
                              "document.getElementById('mPreview').naturalWidth > 0", arg=want)
        app.keyboard.press("Escape")
    app.click("#mmfList li[data-src='mmf']")  # clicking it again shows everything
    app.wait_for_selector("#grid .card:not(.mmf)")
    app.click("#creators li[data-all]")

    # Search finds MyMiniFactory items among the local results; no separate strip.
    app.reload()
    app.fill("#search", "goblin")
    app.wait_for_function("document.querySelectorAll('#grid .card.mmf').length === 1")
    app.wait_for_selector("#grid .card.mmf[data-mmf='6']")
    assert app.locator("#grid .card.mmf").count() == 1 and app.locator("#grid .card:not(.mmf)").count() >= 1
    assert app.locator("#mmfStrip").count() == 0
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-search.png")
    app.fill("#search", "")
    app.click("#mmfList li[data-src='local']")  # hide MyMiniFactory
    app.wait_for_function("!document.querySelector('#grid .card.mmf') && document.querySelector('#grid .card')")
    app.click("#mmfList li[data-src='missing']")
    app.wait_for_function("(t) => document.getElementById('crumbs').textContent.includes(t)", arg='Not in your library')
    app.wait_for_function("document.querySelectorAll('#grid .card.mmf').length === 4")
    assert app.locator("#grid .card:not(.mmf)").count() == 0
    app.wait_for_function("[...document.querySelectorAll('#grid img')].every((i) => i.complete && i.naturalWidth)")
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-grid.png")

    # MyMiniFactory items open in the model window with all their pictures.
    app.click("#grid .card.mmf[data-mmf='2']")
    app.wait_for_selector("#modelDlg.mmf-mode[open]")
    wyrm_images = get("/api/mmf/2")["images"]
    assert [u.rsplit("/", 1)[1] for u in wyrm_images] == [
        "1000X1000-wyrm-a.png", "1000X1000-wyrm-b.png", "1000X1000-wyrm-c.png", "1000X1000-wyrm-d.png"], wyrm_images
    assert get("/api/mmf/1")["images"] == [f"{CDN}/1.png"], "pages without images keep the library picture"
    assert app.locator("#mFiles [data-mmf-pic]").count() == 4
    assert app.locator("#mEditBtn").is_hidden() and app.locator("#mMapBtn").is_hidden()
    app.wait_for_function("document.getElementById('mPreview').naturalWidth > 0")
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-modal.png")
    # Files come from MyMiniFactory itself: a link to the item, then the import dialog ready for it.
    assert app.get_attribute("#mFiles a.button-like.primary", "href") == "https://www.myminifactory.com/object/3d-print-2"
    app.click("#mmfImport")
    app.wait_for_selector("#importDlg[open]")
    assert app.input_value("#uploadFolder") == "Ancient Wyrm" and app.input_value("#uploadCreator") == "Dragon Forge"
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-import.png")
    app.keyboard.press("Escape")

    popup.close()
    # A later sync where purchases drop an item replaces them; pledges failing keeps the old ones.
    PURCHASES.pop(1)
    PLEDGES.clear()
    ctx.unroute("https://www.myminifactory.com/**")
    ctx.route("https://www.myminifactory.com/**", lambda r: r.fulfill(status=500, body="")
              if "/data-library/campaigns" in r.request.url else fake_mmf(r))
    with ctx.expect_page() as popup_info:
        mmf.evaluate(urllib.request.unquote(href[len("javascript:"):]))
    popup = popup_info.value
    popup.wait_for_function("document.getElementById('msg').textContent.startsWith('Synced')", timeout=20000)
    print("second sync:", popup.text_content("#msg"))
    s = get("/api/mmf/status")
    assert s["sources"] == {"purchase": 2, "pledge": 1, "tribe": 2}, s

    # The whole library from the new list: every source, release names, one item in two sources.
    popup.close()
    fantasy = {t["tag"].casefold(): t["models"] for t in get("/api/tags")}.get("fantasy", 0)
    NEW_API.append(1)
    def sync(done="Synced"):
        with ctx.expect_page() as info:
            mmf.evaluate(urllib.request.unquote(href[len("javascript:"):]))
        win = info.value
        win.wait_for_function("(t) => document.getElementById('msg').textContent.includes(t) && "
                              "!document.getElementById('msg').textContent.includes('Reading')", arg=done, timeout=30000)
        print("sync:", win.text_content("#msg"))
        return win
    popup = sync()
    s = get("/api/mmf/status")
    assert s["total"] == 7 and s["sources"] == {"purchase": 2, "tribe": 2, "group": 1, "mmfplus": 1, "free": 1, "pledge": 1}, s
    items = {m["id"]: m for m in get("/api/mmf")["items"]}
    assert items[5]["sources"] == [{"source": "tribe", "collection": "MiniForge's Tribe · 09/2023 | tier: Elders"}], items[5]
    assert items[11]["sources"][0]["collection"] == "38. OPR April 2023 Rewards", items[11]
    assert items[14]["sources"][0]["collection"] == "Frog Kingdom", items[14]  # bought through the campaign
    assert items[15]["sources"][0]["collection"] == "Shroudborne", items[15]   # part of a store bundle
    assert items[12]["sources"][0]["collection"] == "September 2023 MMF+ Release", items[12]
    assert sorted((x["source"], x["collection"]) for x in items[13]["sources"]) == [("free", ""), ("pledge", "Frog Kingdom")]
    assert items[1]["url"] == "https://www.myminifactory.com/object/3d-print-thing-1", items[1]
    assert items[11]["creator"] == "Orc Works" and items[11]["image"].endswith("/230X230-p.png"), items[11]
    assert items[1]["local"], "still matches the local Lich King"

    # Tags from MyMiniFactory: on the items, and on the local models they match (not editable there).
    assert items[5]["tags"] == ["Goblin", "Fantasy"] and items[11]["tags"] == ["orc", "Warhammer"], (items[5], items[11])
    boss = get("/api/models?q=goblin%20boss&source=local")["items"][0]
    assert boss["mmf_tags"] == ["Goblin", "Fantasy"] and get(f"/api/models/{boss['id']}")["mmf_tags"] == ["Goblin", "Fantasy"], boss
    assert boss["id"] in [m["id"] for m in get("/api/models?tags=fantasy")["items"]]
    assert boss["id"] in [m["id"] for m in get("/api/models?q=tag:goblin")["items"]]
    assert [m["id"] for m in get("/api/models?tags=orc")["items"]] == [11]
    assert [m["id"] for m in get("/api/models?q=tag:Warhammer")["items"]] == [11]
    assert 11 in [m["id"] for m in get("/api/models?q=warham")["items"]]
    assert get("/api/models?tags=orc&source=local")["total"] == 0
    tag_counts = {t["tag"].casefold(): t["models"] for t in get("/api/tags")}
    assert tag_counts["fantasy"] == fantasy + 1 and tag_counts["orc"] == 1 and tag_counts["goblin"] == 1, tag_counts
    assert [t["tag"] for t in get("/api/tags")].count("Goblin") == 1
    assert {r["release"] for r in get("/api/releases?tags=orc")} == {"38. OPR April 2023 Rewards"}
    assert {c["creator"]: c["mmf"] for c in get("/api/creators?tags=orc")} == {"Orc Works": 1}
    app.reload()
    app.wait_for_selector("#tagList [data-tag]")
    if app.locator(".side-filter input[data-for='tagList']").is_visible():  # long lists show the first few
        app.fill(".side-filter input[data-for='tagList']", "orc")
    app.click("#tagList [data-tag='orc']")
    app.wait_for_function("document.querySelectorAll('#grid .card').length === 1")
    assert app.locator("#grid .card.mmf[data-mmf='11'] .badge.tag").count() == 2
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-tags.png")

    browser.close()

print("MMF OK")
