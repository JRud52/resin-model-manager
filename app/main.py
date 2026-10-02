from __future__ import annotations

import logging
import mimetypes
import threading
import time
from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, archives, classify, config, db, library, mmf, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = FastAPI(title="Resin Model Manager", version=__version__)
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def startup():
    db.init()
    for d in (config.LIBRARY_DIR, config.CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for i in range(settings.MAX_WORKERS):
        threading.Thread(target=library.preview_worker, args=(i,), daemon=True).start()
    if library.migrate_layouts():
        library.request_reindex()
    else:
        library.reclassify()  # database only: applies grouping and picture-matching changes on upgrade
    library._preview_wakeup.set()


def _sup(v):
    return None if v is None else bool(v)


# ---------------------------------------------------------------- status & jobs

@app.get("/api/status")
def status():
    c = db.conn()
    counts: dict[str, int] = {}
    for table in ("files", "images"):
        for r in c.execute(f"SELECT preview, COUNT(*) n FROM {table} WHERE hidden=0 GROUP BY preview"):
            counts[r["preview"]] = counts.get(r["preview"], 0) + r["n"]
    return {
        "job": library.job.as_dict(),
        "files": c.execute("SELECT COUNT(*) FROM files WHERE hidden=0").fetchone()[0],
        "models": c.execute("SELECT COUNT(DISTINCT model_id) FROM files WHERE hidden=0").fetchone()[0],
        "releases": c.execute("SELECT COUNT(DISTINCT release) FROM files WHERE hidden=0").fetchone()[0],
        "previews": counts,
        "rendering": library.preview_state["current"],
        "source_dir": str(config.SOURCE_DIR),
        "source_available": library.source_available(),
        "library_dir": str(config.LIBRARY_DIR),
        "version": __version__,
        "commit": config.GIT_COMMIT,
    }


@app.get("/api/settings")
def get_settings():
    return settings.values()


@app.put("/api/settings")
def put_settings(changes: dict[str, Union[int, str]]):
    try:
        changed = settings.update(changes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    library.apply_settings_change(changed)
    return settings.values()


@app.post("/api/import")
def start_import():
    if not library.run_job("import", library.import_library):
        raise HTTPException(409, "Another job is running")
    return {"ok": True}


@app.put("/api/upload")
async def upload(request: Request, path: str, size: int = -1, depth: int = Query(0, ge=0, le=1)):
    """Save one file (raw request body) at `path` inside the library.
    depth=1 when `path` starts with a creator folder (Creator/Release/...)."""
    try:
        return await library.save_upload(path, size, request.stream(), depth)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/index")
def start_index():
    """Index the library after uploads (queued if another job is running)."""
    library.request_reindex()
    return {"ok": True}


@app.post("/api/previews/retry")
def retry_previews():
    for table in ("files", "images"):
        db.conn().execute(f"UPDATE {table} SET preview='pending', preview_error=NULL WHERE preview='error'")
    db.conn().commit()
    library._preview_wakeup.set()
    return {"ok": True}


# ---------------------------------------------------------------- browsing

@app.get("/api/releases")
def releases(q: str = "", tags: str = "", creator: Optional[str] = None):
    c = db.conn()
    where, args = _filters(None, q, [t for t in tags.split(",") if t.strip()], creator)
    rows = c.execute(f"""SELECT release, MAX(creator) creator, COUNT(DISTINCT model_id) models, COUNT(*) files,
                         SUM(size) size FROM files WHERE {where} GROUP BY release ORDER BY release COLLATE NOCASE""",
                     args).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/releases/images")
def release_images(release: str):
    """Preview pictures that came with a release (not tied to one model)."""
    return library.release_images(release)


def _filters(release: Optional[str], q: str, tags: list[str], creator: Optional[str] = None):
    where, args = ["hidden=0"], []
    if release is not None:
        where.append("release = ?")
        args.append(release)
    if creator is not None:  # '' selects releases without a creator
        where.append("creator = ? COLLATE NOCASE")
        args.append(creator)
    if q:
        for word in q.split():
            if word.lower().startswith("tag:") and len(word) > 4:  # exact tag search
                where.append("model_id IN (SELECT model_id FROM model_tags WHERE tag = ?)")
                args.append(word[4:])
                continue
            where.append("(model LIKE ? OR release LIKE ? OR creator LIKE ? OR logical_path LIKE ? "
                         "OR model_id IN (SELECT model_id FROM model_tags WHERE tag LIKE ?))")
            args += [f"%{word}%"] * 5
    for t in tags:  # every selected tag must be present
        where.append("model_id IN (SELECT model_id FROM model_tags WHERE tag = ?)")
        args.append(t)
    return " AND ".join(where), args


def _tags_for(model_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {m: [] for m in model_ids}
    c = db.conn()
    for i in range(0, len(model_ids), 500):
        chunk = model_ids[i:i + 500]
        for r in c.execute("SELECT model_id, tag FROM model_tags WHERE model_id IN (%s) ORDER BY tag COLLATE NOCASE"
                           % ",".join("?" * len(chunk)), chunk):
            out[r["model_id"]].append(r["tag"])
    return out


def _clean_tags(tags: list[str]) -> list[str]:
    out = []
    for t in tags:
        t = " ".join(t.split())
        if t and len(t) <= 60 and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out


@app.get("/api/models")
def models(release: Optional[str] = None, q: str = "", supported: Optional[str] = None,
           tags: str = "", creator: Optional[str] = None, offset: int = 0, limit: int = Query(200, le=1000)):
    c = db.conn()
    where, args = _filters(release, q, [t for t in tags.split(",") if t.strip()], creator)
    having = ""
    if supported == "yes":
        having = "HAVING SUM(supported = 1) > 0"
    elif supported == "no":
        having = "HAVING SUM(supported = 0) > 0"
    elif supported == "unknown":
        having = "HAVING SUM(supported IS NULL) > 0"
    total = c.execute(f"SELECT COUNT(*) FROM (SELECT model_id FROM files WHERE {where} GROUP BY model_id {having})",
                      args).fetchone()[0]
    rows = c.execute(f"""SELECT model_id, MIN(model) model, MIN(release) release, MAX(creator) creator,
                COUNT(*) files, SUM(size) size,
                SUM(supported = 1) supported_files, SUM(supported = 0) unsupported_files,
                SUM(ext = '.stl') stl_files, COUNT(DISTINCT NULLIF(option, '')) options,
                GROUP_CONCAT(DISTINCT ext) exts
                FROM files WHERE {where} GROUP BY model_id {having}
                ORDER BY MIN(release) COLLATE NOCASE, MIN(model) COLLATE NOCASE LIMIT ? OFFSET ?""",
                     args + [limit, offset]).fetchall()
    covers = library.cover_file_ids([r["model_id"] for r in rows])
    images = library.cover_image_ids([r["model_id"] for r in rows])
    tag_map = _tags_for([r["model_id"] for r in rows])
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = d.pop("model_id")
        d["cover"] = covers.get(d["id"])
        d["cover_image"] = images.get(d["id"])
        d["tags"] = tag_map.get(d["id"], [])
        d["exts"] = sorted((d["exts"] or "").split(","))
        out.append(d)
    return {"total": total, "items": out}


def _file_dict(r):
    return {
        "id": r["id"], "name": r["name"], "ext": r["ext"], "size": r["size"],
        "path": r["logical_path"], "archive": r["rel_path"] if r["member"] else None,
        "option": r["option"], "supported": _sup(r["supported"]), "preview": r["preview"],
        "preview_error": r["preview_error"], "model_root": r["model_root"], "hidden": bool(r["hidden"]),
    }


@app.get("/api/models/{model_id}")
def model_detail(model_id: str, include_hidden: bool = False):
    c = db.conn()
    rows = c.execute("SELECT * FROM files WHERE model_id=? " + ("" if include_hidden else "AND hidden=0 ") +
                     "ORDER BY option, supported, name COLLATE NOCASE", (model_id,)).fetchall()
    if not rows:
        raise HTTPException(404)
    covers = library.cover_file_ids([model_id])
    main_file = library.main_file_ids([model_id]).get(model_id)
    return {
        "id": model_id, "model": rows[0]["model"], "release": rows[0]["release"], "creator": rows[0]["creator"],
        "cover": covers.get(model_id),
        "main_file": main_file,
        "images": library.model_images(model_id, rows[0]["release"]),
        "tags": _tags_for([model_id])[model_id],
        "roots": sorted({r["model_root"] for r in rows}),
        "combine": library.combine_for(rows[0]["release"], rows[0]["model"]),
        "files": [_file_dict(r) for r in rows],
    }


@app.get("/api/files/{file_id}/preview")
def file_preview(file_id: int):
    p = library.preview_for(file_id)
    if not p:
        raise HTTPException(404, "No preview")
    return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/images/{image_id}/preview")
def image_preview(image_id: int):
    p = library.preview_for(image_id, "images")
    if not p:
        raise HTTPException(404, "No preview")
    return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/images/{image_id}/full")
def image_full(image_id: int):
    """The bundled picture as it is, straight out of its archive if need be."""
    r = db.conn().execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    return _send(r, inline=True)


class MainPictureIn(BaseModel):
    kind: str                       # release | model
    key: str                        # release name or model id
    image_id: Optional[int] = None  # None goes back to the automatic pick
    file_id: Optional[int] = None   # model only: a file's rendered preview instead of an image


@app.put("/api/main-picture")
def main_picture(m: MainPictureIn):
    """Pick the picture shown first for a release or a model."""
    if m.kind not in ("release", "model") or not m.key:
        raise HTTPException(400, "kind must be release or model, with a key")
    if m.file_id is not None and m.kind != "model":
        raise HTTPException(400, "Only a model's main image can be a file preview")
    try:
        library.set_main_picture(m.kind, m.key, m.image_id, m.file_id)
    except KeyError:
        raise HTTPException(404, "No such picture")
    return {"ok": True}


@app.get("/api/files/{file_id}/download")
def file_download(file_id: int):
    return _send(db.conn().execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone())


def _send(r, inline: bool = False):
    if not r:
        raise HTTPException(404)
    path = (config.LIBRARY_DIR / r["rel_path"]).resolve()
    if not path.is_relative_to(config.LIBRARY_DIR.resolve()) or not path.exists():
        raise HTTPException(404)
    mime = "model/stl" if r["ext"] == ".stl" else (mimetypes.guess_type(r["name"])[0] or "application/octet-stream")
    disposition = "inline" if inline else "attachment"
    if not r["member"]:
        return FileResponse(path, media_type=mime, filename=r["name"], content_disposition_type=disposition)
    stream = archives.open_member_stream(path, r["member"])

    def gen():
        with stream:
            while chunk := stream.read(1 << 20):
                yield chunk

    return StreamingResponse(gen(), media_type=mime, headers={
        "Content-Disposition": f'{disposition}; filename="{r["name"]}"', "Content-Length": str(r["size"])})


# ---------------------------------------------------------------- tags

class TagsIn(BaseModel):
    add: list[str] = []
    remove: list[str] = []
    release: Optional[str] = None  # for the bulk release endpoint


def _apply_tags(model_ids: list[str], t: TagsIn):
    c = db.conn()
    add, remove = _clean_tags(t.add), _clean_tags(t.remove)
    # Reuse the spelling a tag already has, so "Sci-fi" and "sci-fi" stay one tag.
    add = [(c.execute("SELECT tag FROM model_tags WHERE tag=? LIMIT 1", (a,)).fetchone() or [a])[0] for a in add]
    for mid in model_ids:
        for tag in add:
            c.execute("INSERT OR IGNORE INTO model_tags(model_id, tag) VALUES (?,?)", (mid, tag))
        for tag in remove:
            c.execute("DELETE FROM model_tags WHERE model_id=? AND tag=?", (mid, tag))
    c.commit()


@app.get("/api/tags")
def all_tags(release: Optional[str] = None):
    c = db.conn()
    where, args = "f.hidden=0", []
    if release is not None:
        where += " AND f.release=?"
        args.append(release)
    rows = c.execute(f"""SELECT t.tag, COUNT(DISTINCT t.model_id) models FROM model_tags t
                         JOIN files f ON f.model_id = t.model_id WHERE {where}
                         GROUP BY t.tag COLLATE NOCASE ORDER BY t.tag COLLATE NOCASE""", args).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/models/{model_id}/tags")
def model_tags(model_id: str, t: TagsIn):
    _apply_tags([model_id], t)
    return {"tags": _tags_for([model_id])[model_id]}


@app.post("/api/releases/tags")
def release_tags(t: TagsIn):
    """Add or remove tags on every model in a release."""
    if t.release is None:
        raise HTTPException(400, "release is required")
    ids = [r[0] for r in db.conn().execute(
        "SELECT DISTINCT model_id FROM files WHERE release=? AND hidden=0", (t.release,))]
    _apply_tags(ids, t)
    return {"models": len(ids)}


# ---------------------------------------------------------------- creators

@app.get("/api/creators")
def creators(q: str = "", tags: str = ""):
    """Every creator with its release and model counts ('' = releases without one)."""
    where, args = _filters(None, q, [t for t in tags.split(",") if t.strip()])
    rows = db.conn().execute(f"""SELECT MIN(creator) creator, COUNT(DISTINCT release) releases,
                                 COUNT(DISTINCT model_id) models FROM files WHERE {where}
                                 GROUP BY creator COLLATE NOCASE ORDER BY creator COLLATE NOCASE""", args).fetchall()
    return [dict(r) for r in rows]


class CreatorIn(BaseModel):
    releases: list[str]
    creator: str = ""  # blank goes back to the folder guess or import rule


@app.post("/api/releases/creator")
def set_release_creator(body: CreatorIn):
    """Set the creator of one or more releases. Kept across re-indexing."""
    creator = _set_creator(body.releases, body.creator)
    library.reclassify()
    return {"releases": len(body.releases), "creator": creator}


def _set_creator(releases: list[str], creator: str) -> str:
    creator = " ".join(creator.split())
    if len(creator) > 200:
        raise HTTPException(400, "Creator name is too long")
    c = db.conn()
    if creator:  # reuse the spelling a creator already has
        row = c.execute("SELECT creator FROM files WHERE creator = ? COLLATE NOCASE AND hidden=0 LIMIT 1",
                        (creator,)).fetchone()
        creator = row[0] if row else creator
    for release in releases:
        if creator:
            c.execute("INSERT INTO release_creators(release, creator) VALUES (?, ?) "
                      "ON CONFLICT(release) DO UPDATE SET creator=excluded.creator", (release, creator))
        else:
            c.execute("DELETE FROM release_creators WHERE release = ?", (release,))
    c.commit()
    return creator


# ---------------------------------------------------------------- corrections

class OverrideIn(BaseModel):
    prefixes: list[str]
    release: Optional[str] = None
    model: Optional[str] = None
    option: Optional[str] = None
    creator: Optional[str] = None
    supported: Optional[int] = None  # 1 / 0 / -1 (unknown)
    hidden: Optional[int] = None
    from_model_id: Optional[str] = None  # when renaming a whole model, its tags move with it


@app.post("/api/overrides")
def add_override(o: OverrideIn):
    c = db.conn()
    prefixes = [p.strip().strip("/") for p in o.prefixes]
    if not all(prefixes):
        raise HTTPException(400, "Empty prefix")
    library.add_overrides(prefixes, {k: getattr(o, k) for k in library._OVERRIDE_FIELDS})
    library.reclassify()
    if o.from_model_id and o.model is not None and o.release is not None:
        new_id = library.model_id(o.release, o.model)
        if new_id != o.from_model_id:
            c.execute("INSERT OR IGNORE INTO model_tags(model_id, tag) SELECT ?, tag FROM model_tags WHERE model_id=?",
                      (new_id, o.from_model_id))
            c.execute("DELETE FROM model_tags WHERE model_id=?", (o.from_model_id,))
            c.execute("UPDATE OR IGNORE main_pictures SET key=? WHERE kind='model' AND key=?",
                      (new_id, o.from_model_id))
            c.commit()
    library._preview_wakeup.set()
    return {"ok": True}


@app.get("/api/overrides")
def list_overrides():
    return [dict(r) for r in db.conn().execute("SELECT * FROM overrides ORDER BY prefix")]


@app.delete("/api/overrides/{oid}")
def delete_override(oid: int):
    db.conn().execute("DELETE FROM overrides WHERE id=?", (oid,))
    db.conn().commit()
    library.reclassify()
    return {"ok": True}


# ---------------------------------------------------------------- folder mappings

class MapIn(BaseModel):
    prefix: str
    roles: list[str]


def _check_map(m: MapIn) -> tuple[str, tuple]:
    prefix = m.prefix.strip().strip("/")
    if not prefix:
        raise HTTPException(400, "Pick a folder to apply the mapping to")
    try:
        roles = classify.check_roles(m.roles)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return prefix, roles


@app.get("/api/maps")
def list_maps():
    counts = {}
    rows = db.conn().execute("SELECT logical_path FROM files").fetchall()
    maps = library.path_maps()
    for m in maps:
        counts[m["id"]] = sum(1 for r in rows if library._under(r["logical_path"], m["prefix"]))
    return [{**m, "files": counts[m["id"]]} for m in maps]


@app.get("/api/maps/suggest")
def suggest_map(path: str):
    """The folder roles a path is read with now (from its mapping or the automatic guess)."""
    return library.map_suggestion(path)


@app.post("/api/maps/preview")
def preview_map(m: MapIn):
    return library.preview_map(*_check_map(m))


@app.post("/api/maps")
def save_map(m: MapIn):
    library.save_map(*_check_map(m))
    return {"ok": True}


@app.delete("/api/maps/{map_id}")
def delete_map(map_id: int):
    if not library.delete_map(map_id):
        raise HTTPException(404)
    return {"ok": True}


# ---------------------------------------------------------------- bulk edit

class BulkIn(BaseModel):
    model_ids: list[str]
    add_tags: list[str] = []
    remove_tags: list[str] = []
    release: Optional[str] = None   # move them to this release
    creator: Optional[str] = None   # creator of the release(s) they end up in; '' = back to the guess
    supported: Optional[int] = None  # 1 / 0 / -1 (unknown)
    hidden: Optional[int] = None


@app.post("/api/models/bulk")
def bulk_edit(b: BulkIn):
    """Edit several models at once (tags first, so they move with the models)."""
    if not b.model_ids:
        raise HTTPException(400, "Pick some models")
    release = " ".join(b.release.split()) if b.release is not None else None
    if b.supported not in (None, 1, 0, -1):
        raise HTTPException(400, "Bad support value")
    _apply_tags(b.model_ids, TagsIn(add=b.add_tags, remove=b.remove_tags))
    releases = library.bulk_edit(b.model_ids, release or None, b.supported, 1 if b.hidden else None)
    if b.creator is not None and releases:
        _set_creator(releases, b.creator)
        library.reclassify()
    return {"models": len(b.model_ids), "releases": releases}


# ---------------------------------------------------------------- combined models

class CombineIn(BaseModel):
    release: str
    name: str
    model_ids: list[str] = []


def _combine_name(name: str) -> str:
    name = " ".join(name.split())
    if not name or len(name) > 200:
        raise HTTPException(400, "Give the combined model a name")
    return name


@app.get("/api/combines")
def list_combines():
    return library.combines()


@app.post("/api/combines")
def combine(body: CombineIn):
    """Combine models of a release into one; each becomes an option group."""
    try:
        return library.combine_models(body.release, body.model_ids, _combine_name(body.name))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/combines/{combine_id}")
def update_combine(combine_id: int, body: CombineIn):
    """Rename a combined model or move it to another release."""
    if not library.update_combine(combine_id, _combine_name(body.name), body.release):
        raise HTTPException(404)
    return {"id": library.model_id(body.release, body.name)}


@app.delete("/api/combines/{combine_id}")
def split_combine(combine_id: int):
    if not library.split_combine(combine_id):
        raise HTTPException(404)
    return {"ok": True}


# ---------------------------------------------------------------- MyMiniFactory

@app.get("/api/mmf")
def mmf_items(q: str = "", missing: bool = False, offset: int = 0, limit: int = Query(200, le=1000)):
    """The synced MyMiniFactory library; `local` is the matching local release or model."""
    return mmf.items(q, missing, offset, limit)


@app.get("/api/mmf/status")
def mmf_status():
    return mmf.status()


@app.post("/api/mmf/import")
def mmf_import(payload: dict):
    try:
        return mmf.import_library(payload)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/mmf/queue")
def mmf_queue():
    """What the bookmarklet should download on this run (asked for by the sync window)."""
    return mmf.queue()


@app.get("/api/mmf/{oid}")
def mmf_item(oid: int):
    d = mmf.item(oid)
    if not d:
        raise HTTPException(404)
    return d


class MmfQueueIn(BaseModel):
    queued: bool = True


@app.post("/api/mmf/{oid}/queue")
def mmf_set_queued(oid: int, body: MmfQueueIn):
    """Mark an item to be downloaded into the library the next time the bookmarklet runs."""
    if not mmf.set_queued(oid, body.queued):
        raise HTTPException(404)
    return mmf.item(oid)


class MmfResultIn(BaseModel):
    ok: bool
    note: str = ""


@app.post("/api/mmf/{oid}/downloaded")
def mmf_downloaded(oid: int, body: MmfResultIn):
    mmf.download_result(oid, body.ok, body.note)
    if body.ok:
        library.request_reindex()
    return {"ok": True}


@app.delete("/api/mmf")
def mmf_clear():
    mmf.clear()
    return {"ok": True}


@app.get("/mmf-sync")
def mmf_sync_page():
    """Opened by the sync bookmarklet; receives the library from the MyMiniFactory tab."""
    return FileResponse(STATIC / "mmf-sync.html", headers=NO_CACHE)


@app.get("/api/hidden")
def hidden_files():
    rows = db.conn().execute("SELECT * FROM files WHERE hidden=1 ORDER BY logical_path LIMIT 1000").fetchall()
    return [_file_dict(r) for r in rows]


# Changes with every image, so browsers fetch the new app.js/style.css after an
# update instead of running a cached copy against the new server.
ASSET_VERSION = config.GIT_COMMIT or str(int(time.time()))
NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={ASSET_VERSION}\"")
    return HTMLResponse(html, headers=NO_CACHE)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    # Without Cache-Control browsers guess how long to reuse these files, which
    # can hide an update for days. no-cache still lets them reuse via ETag.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


app.mount("/static", StaticFiles(directory=STATIC), name="static")
