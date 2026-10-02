"""The user's MyMiniFactory library, listed next to the local one.

MyMiniFactory has no API for a user's purchases, pledges or tribes, so the sync
bookmarklet (static/mmf-bookmarklet.js) reads them from the website's own library
page in the user's logged-in browser and posts them here. Nothing is downloaded
and no MyMiniFactory login is ever stored by the app.
"""
from __future__ import annotations

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
        rows[oid] = {
            "name": name,
            "creator": _text(it.get("creator"), 200) or old.get("creator", ""),
            "creator_url": _url(it.get("creator_url")) or old.get("creator_url", ""),
            "url": _url(it.get("url")) or old.get("url", "") or f"https://www.myminifactory.com/object/{oid}",
            "image": _url(it.get("image")) or old.get("image", ""),
        }
        links.add((oid, source, _text(it.get("collection"), 300)))
    now = time.time()
    c = db.conn()
    with c:
        for s in complete:
            c.execute("DELETE FROM mmf_links WHERE source=?", (s,))
        c.executemany("""INSERT INTO mmf_items(id, name, creator, creator_url, url, image, updated)
                         VALUES (:id, :name, :creator, :creator_url, :url, :image, :updated)
                         ON CONFLICT(id) DO UPDATE SET name=excluded.name, creator=excluded.creator,
                         creator_url=excluded.creator_url, url=excluded.url, image=excluded.image,
                         updated=excluded.updated""",
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
        d = dict(r)
        d["sources"] = link_map.get(r["id"], [])
        d["local"] = _match(r["name"], releases, models)
        if missing and d["local"]:
            continue
        out.append(d)
    return {"total": len(out), "items": out[offset:offset + limit]}


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
    return {"total": total, "missing": missing, "sources": by_source,
            "synced": float(synced[0]) if synced else None}
