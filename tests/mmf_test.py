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
PURCHASES = [obj(1, "Lich King - Supported"), obj(2, "Ancient Wyrm", images=WYRM_PICS, archive_download_url="/download/2"), obj(3, "Javascript", absolute_url="javascript:alert(1)")]
PLEDGES = [obj(4, "Siege Tower", pledges={"items": [{"name": "Kickstarter: Castle Siege"}]})]
TRIBE = [obj(5, "Goblin Boss", creator="Greenskins"), obj(6, "Goblin Shaman", creator="Greenskins")]

def png(i):
    from io import BytesIO
    from PIL import Image
    b = BytesIO()
    Image.new("RGB", (64, 64), ((i * 70) % 256, 120, 200 - i * 25)).save(b, "PNG")
    return b.getvalue()


def wyrm_zip():
    import io
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("Wyrm_supported.stl", "solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\n"
                   "vertex 0 10 0\nendloop\nendfacet\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 0 10 0\n"
                   "vertex 0 0 10\nendloop\nendfacet\nendsolid t\n")
    return b.getvalue()


def page_of(items, page, size=2):
    return {"total_count": len(items), "items": items[(page - 1) * size:page * size]}


def fake_mmf(route):
    url = route.request.url
    page = int(url.split("page=")[1]) if "page=" in url else 1
    if "/data-library/purchases" in url:
        body = page_of(PURCHASES, page)
    elif "/data-library/campaigns" in url:
        body = page_of(PLEDGES, page)
    elif "/data-library/tribes" in url:
        body = page_of([{"id": 9, "name": "Greenskin Tribe",
                         "groups": {"items": [{"id": "all/9", "name": "All"}, {"id": "42", "name": "March"}]}}], page)
    elif "/data-library/group/42" in url:
        body = page_of(TRIBE, page)
    elif url.endswith("/download/2"):
        return route.fulfill(status=200, content_type="application/zip", body=wyrm_zip(),
                             headers={"Content-Disposition": 'attachment; filename="Ancient_Wyrm.zip"'})
    elif "/download/" in url:  # what the real site does when a download isn't allowed
        return route.fulfill(status=200, content_type="text/html", body="<html>Log in</html>")
    elif "/data-library/" in url:
        return route.fulfill(status=404, body="{}")
    else:
        return route.fulfill(status=200, content_type="text/html", body="<html><body><h1>Library</h1></body></html>")
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def get(u):
    return json.load(urllib.request.urlopen(BASE + u))


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
    ctx.route("https://www.myminifactory.com/**", fake_mmf)
    ctx.route(f"{CDN}/**", lambda r: r.fulfill(
        status=200, content_type="image/png", body=png(int(r.request.url.rsplit("/", 1)[1].split(".")[0]))))
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

    # Search shows MyMiniFactory matches above the local results.
    app.reload()
    app.fill("#search", "goblin")
    app.wait_for_selector("#mmfStrip:not(.hidden) .mmf-chip")
    assert app.locator("#mmfStrip .mmf-chip").count() == 2
    app.wait_for_function("[...document.querySelectorAll('#mmfStrip img')].every((i) => i.complete && i.naturalWidth)")
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-search.png")
    app.fill("#search", "")
    app.wait_for_selector("#mmfStrip.hidden", state="attached")
    app.click("#mmfList li[data-mmf-missing='1']")
    app.wait_for_selector("#grid .card.mmf")
    assert app.locator("#grid .card.mmf").count() == 4
    app.wait_for_function("[...document.querySelectorAll('#grid img')].every((i) => i.complete && i.naturalWidth)")
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-grid.png")

    # MyMiniFactory items open in the model window with all their pictures.
    app.click("#grid .card.mmf[data-mmf='2']")
    app.wait_for_selector("#modelDlg.mmf-mode[open]")
    assert app.locator("#mFiles [data-mmf-pic]").count() == 3
    assert app.locator("#mEditBtn").is_hidden() and app.locator("#mMapBtn").is_hidden()
    app.wait_for_function("document.getElementById('mPreview').naturalWidth > 0")
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-modal.png")
    app.click("#mmfDownload")
    app.wait_for_selector("#mFiles >> text=Waiting to download")
    if SHOTS:
        app.screenshot(path=f"{SHOTS}/mmf-modal-queued.png")
    app.keyboard.press("Escape")
    post = lambda u, b: urllib.request.urlopen(urllib.request.Request(
        BASE + u, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST"))
    post("/api/mmf/4/queue", {"queued": True})
    assert [q["id"] for q in get("/api/mmf/queue")] == [2, 4]
    assert get("/api/mmf/queue")[0]["folder"] == "Dragon Forge/Ancient Wyrm"

    # The next bookmarklet run downloads them with the MyMiniFactory login and uploads them.
    popup.close()
    with ctx.expect_page() as popup_info:
        mmf.evaluate(urllib.request.unquote(href[len("javascript:"):]))
    popup = popup_info.value
    popup.wait_for_function("document.getElementById('msg').textContent.includes('Downloaded')", timeout=30000)
    print("download sync:", popup.text_content("#msg"))
    for _ in range(60):
        if get("/api/models?q=wyrm")["total"] and not get("/api/status")["job"]["running"]:
            break
        time.sleep(0.5)
    wyrm = get("/api/models?q=wyrm")["items"]
    assert wyrm and wyrm[0]["release"] == "Ancient Wyrm" and wyrm[0]["creator"] == "Dragon Forge", wyrm
    item = get("/api/mmf/2")
    assert item["local"] and not item["queued"] and not item["download_note"], item
    tower = get("/api/mmf/4")
    assert tower["queued"] and "web page" in tower["download_note"], tower
    assert get("/api/mmf/status")["queued"] == 1
    post("/api/mmf/4/queue", {"queued": False})

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
    browser.close()

print("MMF OK")
