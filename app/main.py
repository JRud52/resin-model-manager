from __future__ import annotations

import logging
import mimetypes
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, archives, config, db, library, settings

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
    threading.Thread(target=library.scheduler, daemon=True).start()
    library._preview_wakeup.set()


def _sup(v):
    return None if v is None else bool(v)


# ---------------------------------------------------------------- status & jobs

@app.get("/api/status")
def status():
    c = db.conn()
    counts = {r["preview"]: r["n"] for r in c.execute(
        "SELECT preview, COUNT(*) n FROM files WHERE hidden=0 GROUP BY preview")}
    return {
        "job": library.job.as_dict(),
        "files": c.execute("SELECT COUNT(*) FROM files WHERE hidden=0").fetchone()[0],
        "models": c.execute("SELECT COUNT(DISTINCT model_id) FROM files WHERE hidden=0").fetchone()[0],
        "releases": c.execute("SELECT COUNT(DISTINCT release) FROM files WHERE hidden=0").fetchone()[0],
        "previews": counts,
        "rendering": library.preview_state["current"],
        "source_dir": str(config.SOURCE_DIR),
        "source_available": config.SOURCE_DIR.is_dir(),
        "library_dir": str(config.LIBRARY_DIR),
        "version": __version__,
        "commit": config.GIT_COMMIT,
    }


@app.get("/api/settings")
def get_settings():
    return settings.values()


@app.put("/api/settings")
def put_settings(changes: dict[str, int]):
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


@app.post("/api/scan")
def start_scan():
    if not library.run_job("scan", library.scan):
        raise HTTPException(409, "Another job is running")
    return {"ok": True}


@app.post("/api/previews/retry")
def retry_previews():
    db.conn().execute("UPDATE files SET preview='pending', preview_error=NULL WHERE preview='error'")
    db.conn().commit()
    library._preview_wakeup.set()
    return {"ok": True}


# ---------------------------------------------------------------- browsing

@app.get("/api/releases")
def releases(q: str = "", tags: str = ""):
    c = db.conn()
    where, args = _filters(None, q, [t for t in tags.split(",") if t.strip()])
    rows = c.execute(f"""SELECT release, MAX(creator) creator, COUNT(DISTINCT model_id) models, COUNT(*) files,
                         SUM(size) size FROM files WHERE {where} GROUP BY release ORDER BY release COLLATE NOCASE""",
                     args).fetchall()
    return [dict(r) for r in rows]


def _filters(release: Optional[str], q: str, tags: list[str]):
    where, args = ["hidden=0"], []
    if release is not None:
        where.append("release = ?")
        args.append(release)
    if q:
        for word in q.split():
            if word.lower().startswith("tag:") and len(word) > 4:  # exact tag search
                where.append("model_id IN (SELECT model_id FROM model_tags WHERE tag = ?)")
                args.append(word[4:])
                continue
            where.append("(model LIKE ? OR release LIKE ? OR logical_path LIKE ? "
                         "OR model_id IN (SELECT model_id FROM model_tags WHERE tag LIKE ?))")
            args += [f"%{word}%"] * 4
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
           tags: str = "", offset: int = 0, limit: int = Query(200, le=1000)):
    c = db.conn()
    where, args = _filters(release, q, [t for t in tags.split(",") if t.strip()])
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
    tag_map = _tags_for([r["model_id"] for r in rows])
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = d.pop("model_id")
        d["cover"] = covers.get(d["id"])
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
    return {
        "id": model_id, "model": rows[0]["model"], "release": rows[0]["release"], "creator": rows[0]["creator"],
        "cover": covers.get(model_id),
        "tags": _tags_for([model_id])[model_id],
        "roots": sorted({r["model_root"] for r in rows}),
        "files": [_file_dict(r) for r in rows],
    }


@app.get("/api/files/{file_id}/preview")
def file_preview(file_id: int):
    p = library.preview_for(file_id)
    if not p:
        raise HTTPException(404, "No preview")
    return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/files/{file_id}/download")
def file_download(file_id: int):
    r = db.conn().execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    path = (config.LIBRARY_DIR / r["rel_path"]).resolve()
    if not path.is_relative_to(config.LIBRARY_DIR.resolve()) or not path.exists():
        raise HTTPException(404)
    mime = "model/stl" if r["ext"] == ".stl" else (mimetypes.guess_type(r["name"])[0] or "application/octet-stream")
    if not r["member"]:
        return FileResponse(path, media_type=mime, filename=r["name"])
    stream = archives.open_member_stream(path, r["member"])

    def gen():
        with stream:
            while chunk := stream.read(1 << 20):
                yield chunk

    return StreamingResponse(gen(), media_type=mime, headers={
        "Content-Disposition": f'attachment; filename="{r["name"]}"', "Content-Length": str(r["size"])})


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


# ---------------------------------------------------------------- corrections

class OverrideIn(BaseModel):
    prefixes: list[str]
    release: Optional[str] = None
    model: Optional[str] = None
    option: Optional[str] = None
    supported: Optional[int] = None  # 1 / 0 / -1 (unknown)
    hidden: Optional[int] = None
    from_model_id: Optional[str] = None  # when renaming a whole model, its tags move with it


@app.post("/api/overrides")
def add_override(o: OverrideIn):
    c = db.conn()
    for prefix in o.prefixes:
        prefix = prefix.strip().strip("/")
        if not prefix:
            raise HTTPException(400, "Empty prefix")
        existing = c.execute("SELECT * FROM overrides WHERE prefix=?", (prefix,)).fetchone()
        vals = {k: getattr(o, k) for k in ("release", "model", "option", "supported", "hidden")}
        if existing:
            merged = {k: (vals[k] if vals[k] is not None else existing[k]) for k in vals}
            c.execute("UPDATE overrides SET release=?, model=?, option=?, supported=?, hidden=? WHERE id=?",
                      (*merged.values(), existing["id"]))
        else:
            c.execute("INSERT INTO overrides(prefix, release, model, option, supported, hidden) VALUES (?,?,?,?,?,?)",
                      (prefix, *vals.values()))
    c.commit()
    library.reclassify()
    if o.from_model_id and o.model is not None and o.release is not None:
        new_id = library.model_id(o.release, o.model)
        if new_id != o.from_model_id:
            c.execute("INSERT OR IGNORE INTO model_tags(model_id, tag) SELECT ?, tag FROM model_tags WHERE model_id=?",
                      (new_id, o.from_model_id))
            c.execute("DELETE FROM model_tags WHERE model_id=?", (o.from_model_id,))
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


@app.get("/api/hidden")
def hidden_files():
    rows = db.conn().execute("SELECT * FROM files WHERE hidden=1 ORDER BY logical_path LIMIT 1000").fetchall()
    return [_file_dict(r) for r in rows]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
