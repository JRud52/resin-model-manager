from __future__ import annotations

import logging
import mimetypes
import threading
import time
from pathlib import Path
from typing import Annotated, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, archives, auth, classify, config, db, library, mmf, settings

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


# ---------------------------------------------------------------- who is asking

def _viewer(request: Request) -> auth.Viewer:
    v = getattr(request.state, "viewer", None)
    if v is None:
        raise HTTPException(401, "Log in first")
    return v


def _admin(v: Annotated[auth.Viewer, Depends(_viewer)]) -> auth.Viewer:
    if not v.is_admin:
        raise HTTPException(403, "Only an admin can do that")
    return v


Me = Annotated[auth.Viewer, Depends(_viewer)]
Admin = Annotated[auth.Viewer, Depends(_admin)]
# scope: '' = every model the viewer may see, 'mine' = only their own
SCOPES = ("", "mine")


def _vis(v: auth.Viewer, scope: str = "") -> tuple[str, list]:
    """SQL (on files or images) for what v may see: their own and public models, or everything
    for an admin in admin mode; with scope 'mine' only their own."""
    if scope not in SCOPES:
        raise HTTPException(400, "unknown scope")
    if scope == "mine":
        return "owner = ?", [v.id]
    if v.sees_all:
        return "1", []
    return "(owner = ? OR public = 1)", [v.id]



def _mmf_seen(v: auth.Viewer, scope: str = "") -> tuple:
    """Whose MyMiniFactory items v sees, as mmf.seen_items takes it: like models, their own and
    everyone's public ones (all of them in admin mode), only their own for 'mine'."""
    _vis(v, scope)  # checks the scope
    return v.id, v.sees_all, scope == "mine"


def _mmf_dicts(items: list[dict], v: auth.Viewer) -> list[dict]:
    names = auth.usernames()
    for d in items:
        d.update(_owner_fields(v, d["owner"], d["public"], names))
    return items


def _visible(table: str, row_id: int, v: auth.Viewer):
    where, args = _vis(v)
    r = db.conn().execute(f"SELECT * FROM {table} WHERE id=? AND {where}", (row_id, *args)).fetchone()
    if not r:
        raise HTTPException(404)
    return r


def _model_owner(model_id: str) -> Optional[int]:
    r = db.conn().execute("SELECT MIN(owner) FROM files WHERE model_id=?", (model_id,)).fetchone()
    return r[0] if r else None


def _check_edit(v: auth.Viewer, model_ids: list[str]) -> set[int]:
    """The owners of these models; 403 unless v may change all of them."""
    owners = {r[0] for r in library._ids_in_chunks("SELECT DISTINCT owner FROM files WHERE model_id IN (%s)",
                                                   list(model_ids))}
    if not all(v.can_edit(o) for o in owners):
        raise HTTPException(403, "You can only change your own models")
    return owners


def _check_prefix(v: auth.Viewer, prefix: str):
    """Non-admins can only put correction rules on paths in their own folder."""
    if not v.is_admin and not (v.folder and library._under(prefix, v.folder)):
        raise HTTPException(403, "You can only change your own models")


# ---------------------------------------------------------------- status & jobs

@app.get("/api/status")
def status(request: Request):
    v = getattr(request.state, "viewer", None)
    if v is None:  # the container health check
        return {"ok": True, "version": __version__}
    c = db.conn()
    where, args = _vis(v)
    counts: dict[str, int] = {}
    for table in ("files", "images"):
        for r in c.execute(f"SELECT preview, COUNT(*) n FROM {table} WHERE hidden=0 GROUP BY preview"):
            counts[r["preview"]] = counts.get(r["preview"], 0) + r["n"]
    return {
        "job": library.job.as_dict(),
        "files": c.execute(f"SELECT COUNT(*) FROM files WHERE hidden=0 AND {where}", args).fetchone()[0],
        "models": c.execute(f"SELECT COUNT(DISTINCT model_id) FROM files WHERE hidden=0 AND {where}", args).fetchone()[0],
        "releases": c.execute(f"SELECT COUNT(DISTINCT release) FROM files WHERE hidden=0 AND {where}", args).fetchone()[0],
        "previews": counts,
        "rendering": library.preview_state["current"],
        "source_dir": str(config.SOURCE_DIR),
        "source_available": library.source_available(),
        "library_dir": str(config.LIBRARY_DIR),
        "version": __version__,
        "commit": config.GIT_COMMIT,
    }


@app.get("/api/settings")
def get_settings(v: Me):
    return settings.values()


@app.put("/api/settings")
def put_settings(changes: dict[str, Union[int, str]], v: Admin):
    try:
        changed = settings.update(changes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    library.apply_settings_change(changed)
    return settings.values()


@app.post("/api/import")
def start_import(v: Admin):
    if not library.run_job("import", library.import_library):
        raise HTTPException(409, "Another job is running")
    return {"ok": True}


@app.put("/api/upload")
async def upload(request: Request, v: Me, path: str, size: int = -1, depth: int = Query(0, ge=0, le=1)):
    """Save one file (raw request body) at `path` inside the library, or inside the uploader's
    own folder for anyone but the main admin.
    depth=1 when `path` starts with a creator folder (Creator/Release/...)."""
    folder = "" if v.id == db.MAIN_ADMIN else v.folder
    try:
        return await library.save_upload(path, size, request.stream(), depth, folder)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/index")
def start_index(v: Me):
    """Index the library after uploads (queued if another job is running)."""
    library.request_reindex()
    return {"ok": True}


@app.post("/api/previews/retry")
def retry_previews(v: Admin):
    for table in ("files", "images"):
        db.conn().execute(f"UPDATE {table} SET preview='pending', preview_error=NULL WHERE preview='error'")
    db.conn().commit()
    library._preview_wakeup.set()
    return {"ok": True}


# ---------------------------------------------------------------- browsing

# source: '' = the library and the synced MyMiniFactory items not in it, 'local' = only the library,
# 'mmf' = only MyMiniFactory items, 'missing' = only MyMiniFactory items not in the library.
SOURCES = ("", "local", "mmf", "missing")


def _source(source: str) -> str:
    if source not in SOURCES:
        raise HTTPException(400, "unknown source")
    return source


@app.get("/api/releases")
def releases(v: Me, q: str = "", tags: str = "", creator: Optional[str] = None, source: str = "", scope: str = ""):
    """Releases with their model count; `mmf` counts the MyMiniFactory items shown in them too."""
    c = db.conn()
    source = _source(source)
    out = []
    if source in ("", "local"):
        where, args = _filters(v, scope, None, q, [t for t in tags.split(",") if t.strip()], creator)
        rows = c.execute(f"""SELECT release, MAX(creator) creator, COUNT(DISTINCT model_id) models, COUNT(*) files,
                             SUM(size) size FROM files WHERE {where} GROUP BY release ORDER BY release COLLATE NOCASE""",
                         args).fetchall()
        out = [{**dict(r), "mmf": 0} for r in rows]
    if source != "local":  # everyone's own MyMiniFactory library, in All and My models alike
        online = mmf.releases(_mmf_seen(v, scope), q, creator, source, [t for t in tags.split(",") if t.strip()])
        for r in out:
            hit = online.pop(r["release"].casefold(), None)
            r["mmf"] = hit["mmf"] if hit else 0
        out += [{**v, "models": 0} for v in online.values()]
        out.sort(key=lambda r: r["release"].casefold())
    return out


@app.get("/api/releases/images")
def release_images(release: str, v: Me, scope: str = ""):
    """Preview pictures that came with a release (not tied to one model)."""
    return library.release_images(release, *_vis(v, scope))


def _filters(v: auth.Viewer, scope: str, release: Optional[str], q: str, tags: list[str],
             creator: Optional[str] = None):
    """SQL for the files v may see matching the filters. Tags include those of the MyMiniFactory
    items matched to a model (mmf.local_tags)."""
    vis, args = _vis(v, scope)
    where = ["hidden=0", vis]
    online = mmf.local_tags() if q or tags else {}

    def online_ids(test) -> str:
        ids = [m for m, ts in online.items() if any(test(t.casefold()) for t in ts)]
        args.extend(ids)
        return f" OR model_id IN ({','.join('?' * len(ids))})" if ids else ""

    if release is not None:
        where.append("release = ?")
        args.append(release)
    if creator is not None:  # '' selects releases without a creator
        where.append("creator = ? COLLATE NOCASE")
        args.append(creator)
    if q:
        for word in q.split():
            if word.lower().startswith("tag:") and len(word) > 4:  # exact tag search
                args.append(word[4:])
                tag = word[4:].casefold()
                where.append(f"(model_id IN (SELECT model_id FROM model_tags WHERE tag = ?){online_ids(lambda t: t == tag)})")
                continue
            args += [f"%{word}%"] * 5
            low = word.casefold()
            where.append("(model LIKE ? OR release LIKE ? OR creator LIKE ? OR logical_path LIKE ? "
                         f"OR model_id IN (SELECT model_id FROM model_tags WHERE tag LIKE ?){online_ids(lambda t: low in t)})")
    for t in tags:  # every selected tag must be present
        args.append(t)
        tag = t.casefold()
        where.append(f"(model_id IN (SELECT model_id FROM model_tags WHERE tag = ?){online_ids(lambda x: x == tag)})")
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
def models(v: Me, release: Optional[str] = None, q: str = "", supported: Optional[str] = None,
           tags: str = "", creator: Optional[str] = None, offset: int = 0, limit: int = Query(200, le=1000),
           source: str = "", scope: str = ""):
    """Models in the grid: synced MyMiniFactory items first, newest published first, then the library's
    models by release and name. MyMiniFactory items are mixed in like models (with mmf: true) unless
    `source` says otherwise; they have no supported versions."""
    c = db.conn()
    source = _source(source)
    tag_list = [t for t in tags.split(",") if t.strip()]
    online = [] if source == "local" or supported else _mmf_dicts(mmf.newest_first(
        mmf.grid_items(_mmf_seen(v, scope), q, creator, release, source, tag_list)), v)
    where, args = _filters(v, scope, release, q, tag_list, creator)
    having = ""
    if supported == "yes":
        having = "HAVING SUM(supported = 1) > 0"
    elif supported == "no":
        having = "HAVING SUM(supported = 0) > 0"
    elif supported == "unknown":
        having = "HAVING SUM(supported IS NULL) > 0"
    items = online[offset:offset + limit]
    if source in ("mmf", "missing"):
        return {"total": len(online), "items": items}
    local_total = c.execute(f"SELECT COUNT(*) FROM (SELECT model_id FROM files WHERE {where} GROUP BY model_id {having})",
                            args).fetchone()[0]
    local_offset, local_limit = max(0, offset - len(online)), limit - len(items)
    if local_limit > 0:
        rows = c.execute(f"""{_MODEL_SELECT} WHERE {where} GROUP BY model_id {having}
                             ORDER BY MIN(release) COLLATE NOCASE, MIN(model) COLLATE NOCASE LIMIT ? OFFSET ?""",
                         args + [local_limit, local_offset]).fetchall()
        items += _model_dicts(rows, v)
    return {"total": local_total + len(online), "items": items}


_MODEL_SELECT = """SELECT model_id, MIN(model) model, MIN(release) release, MAX(creator) creator,
                COUNT(*) files, SUM(size) size, MIN(owner) owner, MAX(public) public,
                SUM(supported = 1) supported_files, SUM(supported = 0) unsupported_files,
                SUM(ext = '.stl') stl_files, COUNT(DISTINCT NULLIF(option, '')) options,
                GROUP_CONCAT(DISTINCT ext) exts
                FROM files"""


def _owner_fields(v: auth.Viewer, owner: int, public, names: dict[int, str]) -> dict:
    return {"owner": owner, "owner_name": names.get(owner, ""), "public": bool(public),
            "mine": owner == v.id, "can_edit": v.can_edit(owner)}


def _model_dicts(rows, v: auth.Viewer) -> list[dict]:
    covers = library.cover_file_ids([r["model_id"] for r in rows])
    images = library.cover_image_ids([r["model_id"] for r in rows])
    tag_map = _tags_for([r["model_id"] for r in rows])
    online = mmf.local_tags()
    names = auth.usernames()
    out = []
    for r in rows:
        d = dict(r)
        d.update(_owner_fields(v, r["owner"], r["public"], names))
        d["id"] = d.pop("model_id")
        d["mmf_tags"] = _online_tags(d["id"], tag_map.get(d["id"], []), online)
        d["cover"] = covers.get(d["id"])
        d["cover_image"] = images.get(d["id"])
        d["tags"] = tag_map.get(d["id"], [])
        d["exts"] = sorted((d["exts"] or "").split(","))
        out.append(d)
    return out


def _online_tags(model_id: str, own: list[str], online: dict[str, list[str]]) -> list[str]:
    """Tags of the MyMiniFactory items matched to a model that it doesn't have itself."""
    have = {t.casefold() for t in own}
    return [t for t in online.get(model_id, []) if t.casefold() not in have]


def _file_dict(r):
    return {
        "id": r["id"], "name": r["name"], "ext": r["ext"], "size": r["size"],
        "path": r["logical_path"], "archive": r["rel_path"] if r["member"] else None,
        "option": r["option"], "supported": _sup(r["supported"]), "preview": r["preview"],
        "preview_error": r["preview_error"], "model_root": r["model_root"], "hidden": bool(r["hidden"]),
    }


@app.get("/api/models/{model_id}")
def model_detail(model_id: str, v: Me, include_hidden: bool = False):
    c = db.conn()
    where, args = _vis(v)
    rows = c.execute(f"SELECT * FROM files WHERE model_id=? AND {where} " + ("" if include_hidden else "AND hidden=0 ") +
                     "ORDER BY option, supported, name COLLATE NOCASE", (model_id, *args)).fetchall()
    if not rows:
        raise HTTPException(404)
    owner = rows[0]["owner"]
    covers = library.cover_file_ids([model_id])
    main_file = library.main_file_ids([model_id]).get(model_id)
    return {
        "id": model_id, "model": rows[0]["model"], "release": rows[0]["release"], "creator": rows[0]["creator"],
        **_owner_fields(v, owner, max(r["public"] for r in rows), auth.usernames()),
        "cover": covers.get(model_id),
        "main_file": main_file,
        "images": library.model_images(model_id, rows[0]["release"], owner),
        "tags": (tags := _tags_for([model_id])[model_id]),
        "mmf_tags": _online_tags(model_id, tags, mmf.local_tags()),
        "roots": sorted({r["model_root"] for r in rows}),
        "combine": library.combine_for(rows[0]["release"], rows[0]["model"], owner),
        "files": [_file_dict(r) for r in rows],
    }


@app.get("/api/files/{file_id}/preview")
def file_preview(file_id: int, v: Me):
    _visible("files", file_id, v)
    p = library.preview_for(file_id)
    if not p:
        raise HTTPException(404, "No preview")
    return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/images/{image_id}/preview")
def image_preview(image_id: int, v: Me):
    _visible("images", image_id, v)
    p = library.preview_for(image_id, "images")
    if not p:
        raise HTTPException(404, "No preview")
    return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/images/{image_id}/full")
def image_full(image_id: int, v: Me):
    """The bundled picture as it is, straight out of its archive if need be."""
    return _send(_visible("images", image_id, v), inline=True)


class MainPictureIn(BaseModel):
    kind: str                       # release | model
    key: str                        # release name or model id
    image_id: Optional[int] = None  # None goes back to the automatic pick
    file_id: Optional[int] = None   # model only: a file's rendered preview instead of an image


@app.put("/api/main-picture")
def main_picture(m: MainPictureIn, v: Me):
    """Pick the picture shown first for a release or a model. A release's main picture is kept
    per owner, so each person picks it for their own models in it."""
    if m.kind not in ("release", "model") or not m.key:
        raise HTTPException(400, "kind must be release or model, with a key")
    if m.file_id is not None and m.kind != "model":
        raise HTTPException(400, "Only a model's main image can be a file preview")
    if m.image_id is not None:
        owners = {_visible("images", m.image_id, v)["owner"]}
    elif m.file_id is not None:
        owners = {_visible("files", m.file_id, v)["owner"]}
    elif m.kind == "model":
        owners = {_model_owner(m.key) or v.id}
    else:  # un-picking a release's main picture: the viewer's own, or everyone's for an admin
        owners = {v.id} | ({r[0] for r in db.conn().execute("SELECT DISTINCT owner FROM files WHERE release=?",
                                                             (m.key,))} if v.is_admin else set())
    if m.kind == "model":
        _check_edit(v, [m.key])
    elif not all(v.can_edit(o) for o in owners):
        raise HTTPException(403, "You can only change your own models")
    try:
        for owner in owners:
            key = m.key if m.kind == "model" else library.release_key(m.key, owner)
            library.set_main_picture(m.kind, key, m.image_id, m.file_id)
    except KeyError:
        raise HTTPException(404, "No such picture")
    return {"ok": True}


@app.get("/api/files/{file_id}/download")
def file_download(file_id: int, v: Me):
    return _send(_visible("files", file_id, v))


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


def _spelled(tags: list[str]) -> list[str]:
    """Reuse the spelling a tag already has, so "Sci-fi" and "sci-fi" stay one tag."""
    c = db.conn()
    return [(c.execute("SELECT tag FROM model_tags WHERE tag=? COLLATE NOCASE UNION ALL "
                       "SELECT tag FROM mmf_item_tags WHERE tag=? COLLATE NOCASE LIMIT 1", (a, a)).fetchone() or [a])[0]
            for a in tags]


def _apply_tags(model_ids: list[str], t: TagsIn, mmf_ids: list[int] = (), mmf_owner: int = 0):
    """Tags on local models and on MyMiniFactory items of mmf_owner's library (their own tags in the app)."""
    c = db.conn()
    add, remove = _spelled(_clean_tags(t.add)), _clean_tags(t.remove)
    if mmf_ids:
        mmf.set_tags(mmf_owner, list(mmf_ids), add, remove)
    for mid in model_ids:
        for tag in add:
            c.execute("INSERT OR IGNORE INTO model_tags(model_id, tag) VALUES (?,?)", (mid, tag))
        for tag in remove:
            c.execute("DELETE FROM model_tags WHERE model_id=? AND tag=?", (mid, tag))
    c.commit()


@app.get("/api/tags")
def all_tags(v: Me, release: Optional[str] = None, scope: str = ""):
    """Tags with how many models (and MyMiniFactory items not in the library) have them,
    MyMiniFactory tags included; spellings that differ only in capitals count as one."""
    c = db.conn()
    vis, args = _vis(v, scope)
    where = "f.hidden=0 AND " + vis.replace("owner", "f.owner").replace("public", "f.public")
    if release is not None:
        where += " AND f.release=?"
        args.append(release)
    groups: dict[str, dict] = {}

    def add(tag: str, key):
        groups.setdefault(tag.casefold(), {"tag": tag, "ids": set()})["ids"].add(key)

    for r in c.execute(f"""SELECT DISTINCT t.tag, t.model_id FROM model_tags t
                           JOIN files f ON f.model_id = t.model_id WHERE {where}""", args):
        add(r["tag"], r["model_id"])
    online = mmf.local_tags()
    if online:
        shown = {r[0] for r in c.execute(f"SELECT DISTINCT f.model_id FROM files f WHERE {where}", args)}
        for model_id, tags in online.items():
            if model_id in shown:
                for t in tags:
                    add(t, model_id)
    for d in mmf.grid_items(_mmf_seen(v, scope), release=release):
        for t in d["tags"] + d["mmf_tags"]:
            add(t, ("mmf", d["id"]))
    return [{"tag": g["tag"], "models": len(g["ids"])} for g in sorted(groups.values(), key=lambda g: g["tag"].casefold())]


@app.post("/api/models/{model_id}/tags")
def model_tags(model_id: str, t: TagsIn, v: Me):
    _check_edit(v, [model_id])
    _apply_tags([model_id], t)
    return {"tags": _tags_for([model_id])[model_id]}


@app.post("/api/releases/tags")
def release_tags(t: TagsIn, v: Me, scope: str = ""):
    """Add or remove tags on every model in a release that the viewer may change, and on its
    MyMiniFactory items not in the library."""
    if t.release is None:
        raise HTTPException(400, "release is required")
    where, args = _vis(v, scope if v.is_admin else "mine")
    ids = [r[0] for r in db.conn().execute(
        f"SELECT DISTINCT model_id FROM files WHERE release=? AND hidden=0 AND {where}", (t.release, *args))]
    online = [d["id"] for d in mmf.grid_items((v.id, False, True), release=t.release)]
    _apply_tags(ids, t, online, v.id)
    return {"models": len(ids) + len(online)}


# ---------------------------------------------------------------- creators

@app.get("/api/creators")
def creators(v: Me, q: str = "", tags: str = "", scope: str = ""):
    """Every creator with its release and model counts ('' = releases without one), and how many
    synced MyMiniFactory items are by them (mmf). Creators only on MyMiniFactory are listed too,
    with no releases; names are matched loosely ('CobraMode' == 'Cobra Mode')."""
    where, args = _filters(v, scope, None, q, [t for t in tags.split(",") if t.strip()])
    rows = db.conn().execute(f"""SELECT MIN(creator) creator, COUNT(DISTINCT release) releases,
                                 COUNT(DISTINCT model_id) models FROM files WHERE {where}
                                 GROUP BY creator COLLATE NOCASE ORDER BY creator COLLATE NOCASE""", args).fetchall()
    out = [{**dict(r), "mmf": 0} for r in rows]
    online = mmf.creators(_mmf_seen(v, scope), q, [t for t in tags.split(",") if t.strip()])
    for c in out:
        hit = online.pop(mmf.creator_key(c["creator"]), None)
        c["mmf"] = hit["items"] if hit else 0
    out += [{"creator": v["creator"], "releases": 0, "models": 0, "mmf": v["items"]} for v in online.values()]
    out.sort(key=lambda c: (c["creator"] != "", c["creator"].casefold()))
    return out


class CreatorRenameIn(BaseModel):
    creator: str     # the name as shown now
    name: str = ""   # how to show it; blank undoes earlier renames
    folders: bool = False  # also rename the creator's folders in the library to the new name


@app.get("/api/creators/folders")
def creator_folders(creator: str, v: Admin):
    """The library folders a creator's name comes from (what a rename with folders=true moves)."""
    return {"folders": library.creator_folders(creator)}


@app.post("/api/creators/rename")
def rename_creator(body: CreatorRenameIn, v: Admin):
    """Change how a creator is shown, everywhere (local releases and MyMiniFactory items).
    Stored by the creator's loose name key, so it is kept across re-indexing and syncs.
    With folders, the creator's folders in the library copy are renamed too."""
    if not body.creator.strip():
        raise HTTPException(400, "creator is required")
    moved = []
    if body.folders and body.name.strip():
        folders = library.creator_folders(body.creator)
        new = library.safe_folder_name(body.name)
        if not new:
            raise HTTPException(400, "That name can't be used as a folder name")
        for f in folders:  # check every move before making any
            target = "/".join([*f.split("/")[:-1], new])
            if target.casefold() != f.casefold() and (config.LIBRARY_DIR / target).exists():
                raise HTTPException(409, f"A folder called {target} is already in the library")
        try:
            moved = [library.rename_folder(f, body.name) for f in folders]
        except (ValueError, RuntimeError) as e:
            raise HTTPException(409, str(e))
    name = mmf.rename_creator(body.creator, body.name)
    library.reclassify()
    return {"creator": name, "folders": moved}


class CreatorIn(BaseModel):
    releases: list[str]
    creator: str = ""  # blank goes back to the folder guess or import rule


@app.post("/api/releases/creator")
def set_release_creator(body: CreatorIn, v: Me):
    """Set the creator of one or more releases. Kept across re-indexing."""
    creator = _set_creator(body.releases, body.creator, v)
    library.reclassify()
    return {"releases": len(body.releases), "creator": creator}


def _set_creator(releases: list[str], creator: str, v: auth.Viewer) -> str:
    """Each person's models in a release have their own creator setting: the viewer's own, and for
    an admin everyone's in those releases."""
    creator = " ".join(creator.split())
    if len(creator) > 200:
        raise HTTPException(400, "Creator name is too long")
    c = db.conn()
    if creator:  # reuse the spelling a creator already has
        row = c.execute("SELECT creator FROM files WHERE creator = ? COLLATE NOCASE AND hidden=0 LIMIT 1",
                        (creator,)).fetchone()
        creator = row[0] if row else creator
    for release in releases:
        owners = {v.id}
        if v.is_admin:
            owners |= {r[0] for r in c.execute("SELECT DISTINCT owner FROM files WHERE release = ? COLLATE NOCASE",
                                               (release,))}
        for key in (library.release_key(release, o) for o in owners):
            if creator:
                c.execute("INSERT INTO release_creators(release, creator) VALUES (?, ?) "
                          "ON CONFLICT(release) DO UPDATE SET creator=excluded.creator", (key, creator))
            else:
                c.execute("DELETE FROM release_creators WHERE release = ?", (key,))
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
def add_override(o: OverrideIn, v: Me):
    c = db.conn()
    prefixes = [p.strip().strip("/") for p in o.prefixes]
    if not all(prefixes):
        raise HTTPException(400, "Empty prefix")
    for p in prefixes:
        _check_prefix(v, p)
    owner = None
    if o.from_model_id:
        owner = next(iter(_check_edit(v, [o.from_model_id])), None)
    library.add_overrides(prefixes, {k: getattr(o, k) for k in library._OVERRIDE_FIELDS})
    library.reclassify()
    if owner is not None and o.model is not None and o.release is not None:
        new_id = library.model_id(o.release, o.model, owner)
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
def list_overrides(v: Me):
    return [dict(r) for r in db.conn().execute("SELECT * FROM overrides ORDER BY prefix")
            if v.is_admin or (v.folder and library._under(r["prefix"], v.folder))]


@app.delete("/api/overrides/{oid}")
def delete_override(oid: int, v: Me):
    row = db.conn().execute("SELECT prefix FROM overrides WHERE id=?", (oid,)).fetchone()
    if not row:
        raise HTTPException(404)
    _check_prefix(v, row["prefix"])
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
def list_maps(v: Admin):
    counts = {}
    rows = db.conn().execute("SELECT logical_path FROM files").fetchall()
    maps = library.path_maps()
    for m in maps:
        counts[m["id"]] = sum(1 for r in rows if library._under(r["logical_path"], m["prefix"]))
    return [{**m, "files": counts[m["id"]]} for m in maps]


@app.get("/api/maps/suggest")
def suggest_map(path: str, v: Admin):
    """The folder roles a path is read with now (from its mapping or the automatic guess)."""
    return library.map_suggestion(path)


@app.post("/api/maps/preview")
def preview_map(m: MapIn, v: Admin):
    return library.preview_map(*_check_map(m))


@app.post("/api/maps")
def save_map(m: MapIn, v: Admin):
    library.save_map(*_check_map(m))
    return {"ok": True}


@app.delete("/api/maps/{map_id}")
def delete_map(map_id: int, v: Admin):
    if not library.delete_map(map_id):
        raise HTTPException(404)
    return {"ok": True}


# ---------------------------------------------------------------- bulk edit

class BulkIn(BaseModel):
    model_ids: list[str] = []
    mmf_ids: list[int] = []         # the viewer's MyMiniFactory items: only the tags and public apply
    mmf_refs: list[str] = []        # MyMiniFactory items as "owner:id", other people's too (for admins)
    add_tags: list[str] = []
    remove_tags: list[str] = []
    release: Optional[str] = None   # move them to this release
    creator: Optional[str] = None   # creator of the release(s) they end up in; '' = back to the guess
    supported: Optional[int] = None  # 1 / 0 / -1 (unknown)
    hidden: Optional[int] = None
    public: Optional[bool] = None   # who can see them: everyone, or only their owner
    claim: bool = False             # an admin takes over those of removed accounts first


@app.post("/api/models/bulk")
def bulk_edit(b: BulkIn, v: Me):
    """Edit several models at once (tags first, so they move with the models)."""
    online: dict[int, list[int]] = {v.id: list(b.mmf_ids)} if b.mmf_ids else {}  # owner -> item ids
    for ref in b.mmf_refs:
        try:
            owner, oid = map(int, ref.split(":"))
        except ValueError:
            raise HTTPException(400, "Bad MyMiniFactory item")
        if not v.can_edit(owner):
            raise HTTPException(403, "That is someone else's MyMiniFactory item")
        online.setdefault(owner, []).append(oid)
    n_online = sum(map(len, online.values()))
    if not b.model_ids and not n_online:
        raise HTTPException(400, "Pick some models")
    _check_edit(v, b.model_ids)
    if b.claim and b.model_ids:
        if not v.is_admin:
            raise HTTPException(403, "Only an admin can claim models")
        files = [r["id"] for r in library._ids_in_chunks("SELECT id FROM files WHERE model_id IN (%s)", b.model_ids)]
        library.claim(v.id, b.model_ids)
        b.model_ids = sorted({r["model_id"] for r in library._ids_in_chunks(  # claimed models have new ids
            "SELECT model_id FROM files WHERE id IN (%s)", files)})
    release = " ".join(b.release.split()) if b.release is not None else None
    if b.supported not in (None, 1, 0, -1):
        raise HTTPException(400, "Bad support value")
    _apply_tags(b.model_ids, TagsIn(add=b.add_tags, remove=b.remove_tags))
    for owner, ids in online.items():
        _apply_tags([], TagsIn(add=b.add_tags, remove=b.remove_tags), ids, owner)
        if b.public is not None:
            mmf.set_visibility(owner, ids, b.public)
    if not b.model_ids:
        return {"models": 0, "mmf": n_online, "releases": []}
    if b.public is not None:
        library.set_visibility(b.model_ids, b.public)
    releases = library.bulk_edit(b.model_ids, release or None, b.supported, 1 if b.hidden else None)
    if b.creator is not None and releases:
        _set_creator(releases, b.creator, v)
        library.reclassify()
    return {"models": len(b.model_ids), "mmf": n_online, "releases": releases}


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
def list_combines(v: Me):
    return [cb for cb in library.combines() if v.is_admin or cb["owner"] == v.id]


def _combine_owner(combine_id: int, v: auth.Viewer) -> int:
    row = db.conn().execute("SELECT owner FROM model_combines WHERE id=?", (combine_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if not v.can_edit(row["owner"]):
        raise HTTPException(403, "You can only change your own models")
    return row["owner"]


@app.post("/api/combines")
def combine(body: CombineIn, v: Me):
    """Combine models of a release into one; each becomes an option group."""
    owners = _check_edit(v, body.model_ids)
    if len(owners) > 1:
        raise HTTPException(400, "Only one person's models can be combined")
    try:
        return library.combine_models(body.release, body.model_ids, _combine_name(body.name),
                                      next(iter(owners), v.id))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/combines/{combine_id}")
def update_combine(combine_id: int, body: CombineIn, v: Me):
    """Rename a combined model or move it to another release."""
    owner = _combine_owner(combine_id, v)
    if not library.update_combine(combine_id, _combine_name(body.name), body.release):
        raise HTTPException(404)
    return {"id": library.model_id(body.release, body.name, owner)}


@app.delete("/api/combines/{combine_id}")
def split_combine(combine_id: int, v: Me):
    _combine_owner(combine_id, v)
    if not library.split_combine(combine_id):
        raise HTTPException(404)
    return {"ok": True}


# ---------------------------------------------------------------- MyMiniFactory

class VisibilityIn(BaseModel):
    public: bool


@app.get("/api/mmf")
def mmf_items(v: Me, q: str = "", missing: bool = False, offset: int = 0, limit: int = Query(200, le=1000),
              creator: Optional[str] = None, release: Optional[str] = None):
    """The viewer's synced MyMiniFactory library; `local` is the matching local release or model."""
    return mmf.items(v.id, q, missing, offset, limit, creator, release)


@app.get("/api/mmf/status")
def mmf_status(v: Me):
    return mmf.status(v.id)


@app.post("/api/mmf/import")
def mmf_import(payload: dict, v: Me):
    try:
        return mmf.import_library(payload, v.id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/mmf/queue")
def mmf_queue(v: Me):
    """What the bookmarklet should download on this run (asked for by the sync window)."""
    return mmf.queue(v.id)


@app.get("/api/mmf/gallery")
def mmf_gallery_needed(v: Me):
    """Items the bookmarklet should read full-size images for."""
    return mmf.gallery_needed(v.id)


@app.post("/api/mmf/gallery")
def mmf_set_gallery(payload: dict, v: Me):
    return {"saved": mmf.set_galleries(v.id, payload.get("items") or [])}


def _mmf_item(oid: int, owner: Optional[int], v: auth.Viewer, edit: bool = False) -> dict:
    """A MyMiniFactory item of owner's library (the viewer's by default) as v may see it."""
    owner = v.id if owner is None else owner
    d = mmf.item(owner, oid)
    if not d or not (owner == v.id or v.sees_all or d["public"]):
        raise HTTPException(404)
    if edit and not v.can_edit(owner):
        raise HTTPException(403, "Only its owner or an admin can change it")
    return _mmf_dicts([d], v)[0]


@app.get("/api/mmf/{oid}")
def mmf_item(oid: int, v: Me, owner: Optional[int] = None):
    """An item of the viewer's MyMiniFactory library, or of `owner`'s when it is public."""
    return _mmf_item(oid, owner, v)


class MmfQueueIn(BaseModel):
    queued: bool = True


@app.post("/api/mmf/{oid}/tags")
def mmf_item_tags(oid: int, t: TagsIn, v: Me, owner: Optional[int] = None):
    """The item's own tags, added in the app (its MyMiniFactory tags stay as they are)."""
    d = _mmf_item(oid, owner, v, edit=True)
    _apply_tags([], t, [oid], d["owner"])
    return {"tags": mmf.own_tags(d["owner"], [oid]).get(oid, [])}


@app.put("/api/mmf/{oid}/visibility")
def mmf_item_visibility(oid: int, body: VisibilityIn, v: Me, owner: Optional[int] = None):
    d = _mmf_item(oid, owner, v, edit=True)
    mmf.set_visibility(d["owner"], [oid], body.public)
    return {"public": body.public}


@app.post("/api/mmf/{oid}/queue")
def mmf_set_queued(oid: int, body: MmfQueueIn, v: Me):
    """Mark an item to be downloaded into the library the next time the bookmarklet runs."""
    if not mmf.set_queued(v.id, oid, body.queued):
        raise HTTPException(404)
    return mmf.item(v.id, oid)


class MmfResultIn(BaseModel):
    ok: bool
    note: str = ""


@app.post("/api/mmf/{oid}/downloaded")
def mmf_downloaded(oid: int, body: MmfResultIn, v: Me):
    mmf.download_result(v.id, oid, body.ok, body.note)
    if body.ok:
        library.request_reindex()
    return {"ok": True}


@app.delete("/api/mmf")
def mmf_clear(v: Me):
    mmf.clear(v.id)
    return {"ok": True}


@app.get("/mmf-sync")
def mmf_sync_page():
    """Opened by the sync bookmarklet; receives the library from the MyMiniFactory tab."""
    return FileResponse(STATIC / "mmf-sync.html", headers=NO_CACHE)


@app.get("/api/hidden")
def hidden_files(v: Me):
    where, args = _vis(v, "" if v.is_admin else "mine")
    rows = db.conn().execute(f"SELECT * FROM files WHERE hidden=1 AND {where} ORDER BY logical_path LIMIT 1000",
                             args).fetchall()
    return [_file_dict(r) for r in rows]


# ---------------------------------------------------------------- visibility

@app.put("/api/models/{model_id}/visibility")
def model_visibility(model_id: str, body: VisibilityIn, v: Me):
    """Public: every user can see the model. Private: only its owner (and admins in admin mode)."""
    if _model_owner(model_id) is None:
        raise HTTPException(404)
    _check_edit(v, [model_id])
    library.set_visibility([model_id], body.public)
    return {"public": body.public}


# ---------------------------------------------------------------- accounts

class LoginIn(BaseModel):
    username: str
    password: str


def _login_response(request: Request, token: str) -> JSONResponse:
    resp = JSONResponse({"ok": True})
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=https)
    return resp


@app.get("/api/auth/state")
def auth_state(request: Request):
    v = getattr(request.state, "viewer", None)
    return {"setup": auth.setup_needed(), "user": v.as_dict() if v else None}


@app.post("/api/auth/setup")
def auth_setup(body: LoginIn, request: Request):
    """The first visit makes the main admin account, which owns the library as it is."""
    if not auth.setup_needed():
        raise HTTPException(409, "The admin account is already set up; log in instead")
    try:
        auth.create_user(body.username, body.password, is_admin=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _login_response(request, auth.login(body.username, body.password))


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request):
    token = auth.login(body.username, body.password)
    if not token:
        raise HTTPException(401, "Wrong username or password")
    return _login_response(request, token)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(auth.COOKIE)
    if token:
        auth.logout(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/me")
def me(v: Me):
    """The logged-in user; `others` says whether anyone else has an account."""
    return {**v.as_dict(), "others": len(auth.users()) > 1}


class MeIn(BaseModel):
    default_public: Optional[bool] = None  # all my models public / private (clears per-model choices)
    password: Optional[str] = None
    current_password: str = ""


@app.put("/api/me")
def update_me(body: MeIn, request: Request, v: Me):
    if body.password is not None:
        row = db.conn().execute("SELECT password FROM users WHERE id=?", (v.id,)).fetchone()
        if not auth.check_password(body.current_password, row["password"]):
            raise HTTPException(400, "Your current password isn't right")
        try:
            auth.set_password(v.id, body.password, keep=request.cookies.get(auth.COOKIE))
        except ValueError as e:
            raise HTTPException(400, str(e))
    if body.default_public is not None:
        auth.update_user(v.id, default_public=body.default_public)
        library.clear_visibility(v.id)
        mmf.clear_visibility(v.id)
        library.reclassify()
    return auth.viewer(request.cookies.get(auth.COOKIE)).as_dict()


class AdminModeIn(BaseModel):
    on: bool


@app.put("/api/me/admin-mode")
def admin_mode(body: AdminModeIn, request: Request, v: Admin):
    """Admin mode: see everyone's models, private ones too (for this login)."""
    auth.set_admin_mode(request.cookies.get(auth.COOKIE), body.on)
    return auth.viewer(request.cookies.get(auth.COOKIE)).as_dict()


class UserIn(BaseModel):
    username: str = ""
    password: Optional[str] = None
    is_admin: Optional[bool] = None


@app.get("/api/users")
def list_users(v: Admin):
    c = db.conn()
    counts = {r["owner"]: r["n"] for r in c.execute(
        "SELECT owner, COUNT(DISTINCT model_id) n FROM files WHERE hidden=0 GROUP BY owner")}
    removed: dict[str, int] = {}  # removed accounts' folders with models still to claim
    for r in c.execute("SELECT model_root, COUNT(DISTINCT model_id) n FROM files WHERE owner=? AND hidden=0 "
                       "GROUP BY model_root", (db.REMOVED_USER,)):
        root = library.user_root(r["model_root"]) or r["model_root"]
        removed[root] = removed.get(root, 0) + r["n"]
    return {"users": [{**u, "models": counts.get(u["id"], 0)} for u in auth.users()],
            "removed": [{"folder": f, "models": n} for f, n in sorted(removed.items())]}


@app.post("/api/users")
def add_user(body: UserIn, v: Admin):
    try:
        uid = auth.create_user(body.username, body.password or "", bool(body.is_admin))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": uid}


@app.put("/api/users/{user_id}")
def edit_user(user_id: int, body: UserIn, v: Admin):
    if not db.conn().execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
        raise HTTPException(404)
    try:
        if body.password:
            auth.set_password(user_id, body.password)
        if body.is_admin is not None:
            if user_id == v.id and not body.is_admin:
                raise ValueError("You can't take away your own admin rights")
            auth.update_user(user_id, is_admin=body.is_admin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def remove_user(user_id: int, v: Admin):
    """Remove an account. Their models stay in the library, in their folder, as user_removed's
    (public or private as they were, with their tags) until an admin claims them."""
    c = db.conn()
    before = {r["id"]: r["model_id"] for r in c.execute("SELECT id, model_id FROM files WHERE owner=?", (user_id,))}
    try:
        if not auth.delete_user(user_id):
            raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    c.execute("UPDATE model_combines SET owner=? WHERE owner=?", (db.REMOVED_USER, user_id))
    c.commit()
    library.move_release_keys(user_id, db.REMOVED_USER)
    library._regroup(before)
    mmf.clear(user_id, forget=True)  # their MyMiniFactory list was of their own account
    return {"ok": True}


class ClaimIn(BaseModel):
    model_ids: list[str] = []
    folder: str = ""  # all of a removed account's models (their folder, from /api/users)


@app.post("/api/models/claim")
def claim_models(body: ClaimIn, v: Admin):
    """An admin takes over models of removed accounts: they become theirs, in their library."""
    if not body.model_ids and not body.folder.strip("/"):
        raise HTTPException(400, "Pick the models to claim")
    return {"models": library.claim(v.id, body.model_ids, body.folder.strip("/"))}


# Changes with every image, so browsers fetch the new app.js/style.css after an
# update instead of running a cached copy against the new server.
ASSET_VERSION = config.GIT_COMMIT or str(int(time.time()))
NO_CACHE = {"Cache-Control": "no-cache"}


def _page(name: str) -> HTMLResponse:
    html = (STATIC / name).read_text(encoding="utf-8")
    for asset in ("app.js", "style.css"):
        html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={ASSET_VERSION}\"")
    return HTMLResponse(html, headers=NO_CACHE)


@app.get("/")
def index():
    return _page("index.html")


# Reachable without logging in: the login page's own calls, static files and the health check.
_OPEN = ("/api/auth/", "/static/")


@app.middleware("http")
async def require_login(request: Request, call_next):
    request.state.viewer = auth.viewer(request.cookies.get(auth.COOKIE))
    path = request.url.path
    if request.state.viewer is None and not path.startswith(_OPEN) and path != "/api/status":
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Log in first"}, status_code=401)
        return _page("login.html")
    return await call_next(request)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    # Without Cache-Control browsers guess how long to reuse these files, which
    # can hide an update for days. no-cache still lets them reuse via ETag.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


app.mount("/static", StaticFiles(directory=STATIC), name="static")
