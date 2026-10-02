"""The user's MyMiniFactory library, listed next to the local one.

MyMiniFactory has no API for a user's purchases, pledges or tribes, so the sync
bookmarklet (static/mmf-bookmarklet.js) reads them from the website's own library
page in the user's logged-in browser and posts them here. No MyMiniFactory login
is ever stored by the app. Items marked for download are fetched by the same
bookmarklet (it has the login) on its next run and uploaded into the library.
"""
from __future__ import annotations

import json
import re
import time

from . import classify, db

SOURCES = ("purchase", "pledge", "tribe")
MAX_ITEMS = 50_000


def _text(v, limit: int = 300) -> str:
    return " ".join(str(v or "").split())[:limit]


def _url(v) -> str:
    """Only plain https links are kept (they end up in href and src attributes)."""
    v = str(v or "").strip()
    if v.startswith("//"):
        v = "https:" + v
    elif v.startswith("/"):
        v = "https://www.myminifactory.com" + v
    return v[:1000] if v.startswith("https://") and not any(c in v for c in "\"'<> \n\r\t") else ""


def import_library(payload: dict) -> dict:
    """Store a sync. payload = {"items": [...], "complete": ["purchase", ...]}.

    Items of a source listed in `complete` that are missing from this sync are
    dropped; sources that failed part way keep what earlier syncs found."""
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    if len(items) > MAX_ITEMS:
        raise ValueError("too many items")
    complete = [s for s in payload.get("complete") or [] if s in SOURCES]
    rows, links = {}, set()
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            oid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        name = _text(it.get("name"))
        source = it.get("source")
        if oid <= 0 or not name or source not in SOURCES:
            continue
        old = rows.get(oid, {})
        images = [u for u in (_url(x) for x in (it.get("images") or [])[:60] if isinstance(x, str)) if u]
        image = _url(it.get("image")) or old.get("image", "") or (images[0] if images else "")
        if image and image not in images:
            images.insert(0, image)
        downloads = []
        for d in (it.get("downloads") or [])[:500]:
            u = _url(d.get("url")) if isinstance(d, dict) else ""
            if u:
                downloads.append({"url": u, "name": _text(d.get("name"), 200)})
        rows[oid] = {
            "images": json.dumps(images or json.loads(old.get("images", "[]"))),
            "downloads": json.dumps(downloads or json.loads(old.get("downloads", "[]"))),
            "name": name,
            "creator": _text(it.get("creator"), 200) or old.get("creator", ""),
            "creator_url": _url(it.get("creator_url")) or old.get("creator_url", ""),
            "url": _url(it.get("url")) or old.get("url", "") or f"https://www.myminifactory.com/object/{oid}",
            "image": image,
        }
        links.add((oid, source, _text(it.get("collection"), 300)))
    now = time.time()
    c = db.conn()
    with c:
        for s in complete:
            c.execute("DELETE FROM mmf_links WHERE source=?", (s,))
        c.executemany("""INSERT INTO mmf_items(id, name, creator, creator_url, url, image, images, downloads, updated)
                         VALUES (:id, :name, :creator, :creator_url, :url, :image, :images, :downloads, :updated)
                         ON CONFLICT(id) DO UPDATE SET name=excluded.name, creator=excluded.creator,
                         creator_url=excluded.creator_url, url=excluded.url, image=excluded.image,
                         images=excluded.images, downloads=excluded.downloads, updated=excluded.updated""",
                      [{"id": k, **v, "updated": now} for k, v in rows.items()])
        c.executemany("INSERT OR IGNORE INTO mmf_links(item_id, source, collection) VALUES (?,?,?)", sorted(links))
        c.execute("DELETE FROM mmf_items WHERE id NOT IN (SELECT item_id FROM mmf_links)")
        c.execute("INSERT INTO settings(key, value) VALUES('mmf_synced', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    return {"items": len(rows), **status()}


def clear():
    c = db.conn()
    with c:
        c.execute("DELETE FROM mmf_links")
        c.execute("DELETE FROM mmf_items")
        c.execute("DELETE FROM settings WHERE key='mmf_synced'")


def _local_index() -> tuple[dict, dict]:
    """name key -> local release, and name key -> (release, model_id) for models."""
    releases, models = {}, {}
    for r in db.conn().execute("SELECT release, model, MIN(model_id) model_id FROM files WHERE hidden=0 "
                               "GROUP BY release, model"):
        releases.setdefault(classify.name_key(r["release"]), r["release"])
        models.setdefault(classify.name_key(r["model"]), (r["release"], r["model_id"]))
    releases.pop("", None)
    models.pop("", None)
    return releases, models


def _match(name: str, releases: dict, models: dict):
    key = classify.name_key(name)
    if not key:
        return None
    if key in releases:
        return {"release": releases[key], "model_id": None}
    if key in models:
        release, model_id = models[key]
        return {"release": release, "model_id": model_id}
    return None


def items(q: str = "", missing: bool = False, offset: int = 0, limit: int = 200) -> dict:
    c = db.conn()
    where, args = [], []
    for word in q.split():
        where.append("(i.name LIKE ? OR i.creator LIKE ? OR i.id IN "
                     "(SELECT item_id FROM mmf_links WHERE collection LIKE ?))")
        args += [f"%{word}%"] * 3
    rows = c.execute(f"""SELECT i.* FROM mmf_items i {"WHERE " + " AND ".join(where) if where else ""}
                         ORDER BY i.creator COLLATE NOCASE, i.name COLLATE NOCASE""", args).fetchall()
    link_map: dict[int, list] = {}
    for r in c.execute("SELECT * FROM mmf_links ORDER BY source, collection"):
        link_map.setdefault(r["item_id"], []).append({"source": r["source"], "collection": r["collection"]})
    releases, models = _local_index()
    out = []
    for r in rows:
        d = _item(r)
        d["sources"] = link_map.get(r["id"], [])
        d["local"] = _match(r["name"], releases, models)
        if missing and d["local"]:
            continue
        out.append(d)
    return {"total": len(out), "items": out[offset:offset + limit]}


def _item(r) -> dict:
    d = dict(r)
    d["images"] = json.loads(d.get("images") or "[]") or ([d["image"]] if d.get("image") else [])
    d["downloads"] = len(json.loads(d.get("downloads") or "[]"))
    d["queued"] = bool(d.get("queued"))
    return d


def item(oid: int) -> dict | None:
    c = db.conn()
    r = c.execute("SELECT * FROM mmf_items WHERE id=?", (oid,)).fetchone()
    if not r:
        return None
    d = _item(r)
    d["sources"] = [dict(x) for x in c.execute(
        "SELECT source, collection FROM mmf_links WHERE item_id=? ORDER BY source, collection", (oid,))]
    d["local"] = _match(r["name"], *_local_index())
    return d


def set_queued(oid: int, queued: bool) -> bool:
    c = db.conn()
    with c:
        n = c.execute("UPDATE mmf_items SET queued=?, download_note='' WHERE id=?", (int(queued), oid)).rowcount
    return bool(n)


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _folder_name(s: str) -> str:
    return _UNSAFE.sub("-", s).strip(" .-")[:120] or "MyMiniFactory"


def queue() -> list[dict]:
    """Items to download on the next bookmarklet run, with the library folder each goes to:
    Creator/Item (read as Creator / Release) or just Item when the creator is unknown."""
    out = []
    for r in db.conn().execute("SELECT * FROM mmf_items WHERE queued=1 ORDER BY name COLLATE NOCASE"):
        name = _folder_name(r["name"])
        creator = _folder_name(r["creator"]) if r["creator"] else ""
        out.append({"id": r["id"], "name": r["name"], "url": r["url"],
                     "downloads": json.loads(r["downloads"] or "[]"),
                     "folder": f"{creator}/{name}" if creator else name, "depth": 1 if creator else 0})
    return out


def download_result(oid: int, ok: bool, note: str):
    c = db.conn()
    with c:
        c.execute("UPDATE mmf_items SET queued=?, download_note=? WHERE id=?", (0 if ok else 1, _text(note, 500), oid))


def status() -> dict:
    c = db.conn()
    total = c.execute("SELECT COUNT(*) FROM mmf_items").fetchone()[0]
    synced = c.execute("SELECT value FROM settings WHERE key='mmf_synced'").fetchone()
    by_source = {r["source"]: r["n"] for r in c.execute(
        "SELECT source, COUNT(DISTINCT item_id) n FROM mmf_links GROUP BY source")}
    missing = 0
    if total:
        releases, models = _local_index()
        missing = sum(1 for r in c.execute("SELECT name FROM mmf_items") if not _match(r["name"], releases, models))
    queued = c.execute("SELECT COUNT(*) FROM mmf_items WHERE queued=1").fetchone()[0]
    return {"total": total, "missing": missing, "sources": by_source, "queued": queued,
            "synced": float(synced[0]) if synced else None}
