"""Import, scanning, classification and preview rendering."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
import traceback
from collections import defaultdict
from pathlib import Path, PurePosixPath

from . import archives, classify, config, db, render, settings

log = logging.getLogger("rmm")


# ---------------------------------------------------------------- job state

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.name: str | None = None
        self.message = ""
        self.done = 0
        self.total = 0
        self.error: str | None = None
        self.finished_at: float | None = None
        self.last: dict = {}

    def start(self, name: str) -> bool:
        with self.lock:
            if self.name:
                return False
            self.name, self.message, self.done, self.total, self.error = name, "Starting", 0, 0, None
            return True

    def finish(self, error: str | None = None):
        with self.lock:
            self.last = {"job": self.name, "message": self.message, "error": error, "at": time.time()}
            self.name = None
            self.error = error

    def as_dict(self):
        return {"running": self.name, "message": self.message, "done": self.done,
                "total": self.total, "last": self.last}


job = Job()
_preview_wakeup = threading.Event()
_reindex_wanted = threading.Event()


def run_job(name: str, fn) -> bool:
    if not job.start(name):
        return False

    def wrapper():
        try:
            fn()
            job.finish()
        except Exception as e:  # pragma: no cover - surfaced in UI
            log.exception("%s failed", name)
            job.message = f"Failed: {e}"
            job.finish(traceback.format_exc())
        _preview_wakeup.set()
        if _reindex_wanted.is_set():
            request_reindex()

    threading.Thread(target=wrapper, daemon=True).start()
    return True


def request_reindex():
    """Index the library now, or as soon as the running job finishes."""
    _reindex_wanted.set()
    if run_job("index", scan):
        _reindex_wanted.clear()


# ---------------------------------------------------------------- upload

UPLOAD_EXTS = config.MODEL_EXTS | config.ARCHIVE_EXTS | config.IMAGE_EXTS


def upload_target(rel: str) -> Path:
    """Validate a browser-supplied relative path and return where it goes in the library."""
    parts = [p for p in PurePosixPath(rel.replace("\\", "/")).parts if p not in ("", ".", "/")]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise ValueError("invalid path")
    if os.path.splitext(parts[-1])[1].lower() not in UPLOAD_EXTS:
        raise ValueError(f"{parts[-1]}: not a model file, picture or .zip/.7z archive")
    lib = config.LIBRARY_DIR.resolve()
    target = lib.joinpath(*parts).resolve()
    if not target.is_relative_to(lib):
        raise ValueError("invalid path")
    return target


# ---------------------------------------------------------------- folder layouts

def _top_key(rel: str) -> str | None:
    """The top-level library folder a path belongs to, with archives read as folders
    (``Release.zip`` -> ``Release``); None for a loose file at the library root."""
    parts = PurePosixPath(rel).parts
    if len(parts) > 1:
        return parts[0]
    p = PurePosixPath(rel)
    return p.stem if p.suffix.lower() in config.ARCHIVE_EXTS else None


def folder_layouts() -> dict[str, int]:
    return {r["folder"]: r["depth"] for r in db.conn().execute("SELECT folder, depth FROM folder_layouts")}


def depth_for(logical_path: str, layouts: dict[str, int], default: int) -> int:
    top = PurePosixPath(logical_path).parts[0]
    return layouts.get(top, default)


def record_layout(rel: str, depth: int):
    """Remember how a top-level folder is organised when an upload creates it.

    An existing record is kept. A plain upload into a folder that is already in
    the library without a record (e.g. part of a copied NAS library) records
    nothing, so it keeps following the Settings value."""
    top = _top_key(rel)
    if top is None:
        return
    c = db.conn()
    if c.execute("SELECT 1 FROM folder_layouts WHERE folder = ?", (top,)).fetchone():
        return
    if depth == 0 and (config.LIBRARY_DIR / top).exists():
        return
    c.execute("INSERT INTO folder_layouts(folder, depth) VALUES (?, ?)", (top, depth))
    c.commit()


def _top_entries(root: Path) -> set[str]:
    out = set()
    try:
        for e in root.iterdir():
            if e.name.startswith(".") or e.name in ("@eaDir", "#recycle"):
                continue
            if e.is_dir():
                out.add(e.name)
            elif e.suffix.lower() in config.ARCHIVE_EXTS:
                out.add(e.stem)
    except OSError:
        pass
    return out


def migrate_layouts() -> bool:
    """One-time step for libraries indexed before layouts were recorded per folder:
    top-level folders that are not in the mounted NAS library were uploaded from the
    browser, so they are Release / Model. Returns True when anything was recorded."""
    c = db.conn()
    if c.execute("SELECT 1 FROM settings WHERE key = 'layouts_migrated'").fetchone():
        return False
    if not source_available():
        if c.execute("SELECT 1 FROM files LIMIT 1").fetchone():
            return False  # can't tell copied folders from uploads; try again once SOURCE_PATH is mounted
        changed = False
    else:
        uploaded = _top_entries(config.LIBRARY_DIR) - _top_entries(config.SOURCE_DIR)
        known = set(folder_layouts())
        c.executemany("INSERT INTO folder_layouts(folder, depth) VALUES (?, 0)",
                      [(f,) for f in sorted(uploaded - known)])
        changed = bool(uploaded - known)
    c.execute("INSERT INTO settings(key, value) VALUES ('layouts_migrated', '1')")
    c.commit()
    return changed


async def save_upload(rel: str, size: int, chunks, depth: int = 0) -> dict:
    """Stream an uploaded file into the library. An identical-size file already
    there is kept as is; a different file with the same name gets a numbered name.
    `depth` is the layout of the uploaded path: 0 Release/..., 1 Creator/Release/..."""
    target = upload_target(rel)
    record_layout(target.relative_to(config.LIBRARY_DIR.resolve()).as_posix(), depth)
    if target.exists():
        if target.stat().st_size == size:
            async for _ in chunks:
                pass
            return {"status": "exists", "path": target.relative_to(config.LIBRARY_DIR.resolve()).as_posix()}
        stem, ext, n = target.stem, target.suffix, 2
        while target.exists():
            target = target.with_name(f"{stem} ({n}){ext}")
            n += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".rmm-partial")
    written = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in chunks:
                f.write(chunk)
                written += len(chunk)
        if size >= 0 and written != size:
            raise ValueError(f"upload incomplete ({written} of {size} bytes)")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return {"status": "saved", "path": target.relative_to(config.LIBRARY_DIR.resolve()).as_posix()}


# ---------------------------------------------------------------- import from a NAS folder

def _safe_rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def source_available() -> bool:
    """True when an existing library is mounted at SOURCE_DIR (compose mounts an
    empty placeholder volume there when SOURCE_PATH is not set)."""
    try:
        return config.SOURCE_DIR.is_dir() and any(config.SOURCE_DIR.iterdir())
    except OSError:
        return False


def import_library():
    """Copy SOURCE_DIR into LIBRARY_DIR. Never writes to SOURCE_DIR.

    Files that already exist in the library with the same size and mtime are
    skipped, so re-running an import only copies what is new or changed.
    """
    src, dst = config.SOURCE_DIR.resolve(), config.LIBRARY_DIR.resolve()
    if not source_available():
        raise RuntimeError(f"No existing library is mounted at {src}")
    if src == dst or dst.is_relative_to(src) or src.is_relative_to(dst):
        raise RuntimeError("Source and library folders must be separate")
    job.message = "Counting files in source"
    files = []
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("@eaDir", "#recycle")]
        for n in names:
            if n.startswith(".") or n in ("Thumbs.db", "desktop.ini"):
                continue
            files.append(Path(root) / n)
    job.total = len(files)
    copied = skipped = 0
    for i, f in enumerate(files):
        rel = f.relative_to(src)
        target = dst / rel
        job.done = i
        job.message = f"Copying {rel}"
        try:
            st = f.stat()
            if target.exists():
                tst = target.stat()
                if tst.st_size == st.st_size and int(tst.st_mtime) == int(st.st_mtime):
                    skipped += 1
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".rmm-partial")
            shutil.copyfile(f, tmp)
            os.utime(tmp, (st.st_atime, st.st_mtime))
            os.replace(tmp, target)
            copied += 1
        except OSError as e:
            log.warning("copy failed for %s: %s", f, e)
    job.done = job.total
    job.message = f"Imported {copied} files ({skipped} already up to date)"
    log.info(job.message)
    msg = job.message
    scan()
    job.message = f"{msg}. {job.message}"


# ---------------------------------------------------------------- scan

# Bump when scanning starts keeping a new kind of archive member, so archives
# indexed by an older version are read again (2: preview pictures).
ARCHIVE_LISTING = 2


def scan():
    lib = config.LIBRARY_DIR
    c = db.conn()
    job.message = "Scanning library"
    c.execute("UPDATE files SET seen = 0")
    c.execute("UPDATE images SET seen = 0")
    paths = []
    for root, dirs, names in os.walk(lib):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("@eaDir", "#recycle")]
        for n in names:
            if n.startswith(".") or n.endswith(".rmm-partial"):
                continue
            ext = os.path.splitext(n)[1].lower()
            if ext in config.MODEL_EXTS or ext in config.ARCHIVE_EXTS or ext in config.IMAGE_EXTS:
                paths.append(Path(root) / n)
    job.total = len(paths)
    known_archives = {r["rel_path"]: r for r in c.execute("SELECT * FROM archives")}
    for i, p in enumerate(paths):
        job.done = i
        rel = _safe_rel(p, lib)
        job.message = f"Indexing {rel}"
        st = p.stat()
        ext = p.suffix.lower()
        if ext in config.ARCHIVE_EXTS:
            prev = known_archives.get(rel)
            if (prev and prev["size"] == st.st_size and prev["mtime"] == st.st_mtime and not prev["error"]
                    and prev["listed"] >= ARCHIVE_LISTING):
                c.execute("UPDATE files SET seen = 1 WHERE rel_path = ?", (rel,))
                c.execute("UPDATE images SET seen = 1 WHERE rel_path = ?", (rel,))
                continue
            try:
                members = archives.list_members(p)
                err = None
            except Exception as e:
                log.warning("cannot read archive %s: %s", rel, e)
                members, err = [], str(e)
            c.execute("INSERT OR REPLACE INTO archives(rel_path, size, mtime, error, listed) VALUES (?,?,?,?,?)",
                      (rel, st.st_size, st.st_mtime, err, ARCHIVE_LISTING))
            # Members no longer in the archive stay unseen and are removed below.
            base = str(PurePosixPath(rel).with_suffix(""))
            for m in members:
                mp = PurePosixPath(m.name)
                if mp.is_absolute() or ".." in mp.parts or any(x.startswith("__MACOSX") for x in mp.parts):
                    continue
                mext = mp.suffix.lower()
                if mext in config.MODEL_EXTS:
                    _upsert(c, "files", rel, m.name, f"{base}/{m.name}", mp.name, mext, m.size, st.st_mtime)
                elif mext in config.IMAGE_EXTS and not mp.name.startswith("."):
                    _upsert(c, "images", rel, m.name, f"{base}/{m.name}", mp.name, mext, m.size, st.st_mtime)
        else:
            table = "images" if ext in config.IMAGE_EXTS else "files"
            _upsert(c, table, rel, "", rel, p.name, ext, st.st_size, st.st_mtime)
        if i % 200 == 0:
            c.commit()
    c.execute("DELETE FROM files WHERE seen = 0")
    c.execute("DELETE FROM images WHERE seen = 0")
    c.execute("DELETE FROM archives WHERE error IS NULL AND rel_path NOT IN "
              "(SELECT rel_path FROM files UNION SELECT rel_path FROM images)")
    c.commit()
    job.done = job.total
    reclassify()
    n = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    m = c.execute("SELECT COUNT(DISTINCT model_id) FROM files WHERE hidden = 0").fetchone()[0]
    job.message = f"Indexed {n} files in {m} models"
    _preview_wakeup.set()


def _upsert(c, table, rel, member, logical, name, ext, size, mtime):
    """Add or refresh a row in `files` (model files) or `images` (preview pictures)."""
    row = c.execute(f"SELECT id, size, mtime FROM {table} WHERE rel_path = ? AND member = ?", (rel, member)).fetchone()
    if row and row["size"] == size and row["mtime"] == mtime:
        c.execute(f"UPDATE {table} SET seen = 1 WHERE id = ?", (row["id"],))
        return
    if row:
        c.execute(f"UPDATE {table} SET logical_path=?, name=?, ext=?, size=?, mtime=?, preview='pending', "
                  "preview_error=NULL, seen=1 WHERE id=?", (logical, name, ext, size, mtime, row["id"]))
    else:
        c.execute(f"INSERT INTO {table}(rel_path, member, logical_path, name, ext, size, mtime, seen) "
                  "VALUES (?,?,?,?,?,?,?,1)", (rel, member, logical, name, ext, size, mtime))


# ---------------------------------------------------------------- classification

def model_id(release: str, model: str) -> str:
    key = classify.norm(release) + "\x00" + (classify.strip_tokens(model) or classify.norm(model))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def reclassify():
    """Re-run the heuristics plus user overrides over every indexed file."""
    c = db.conn()
    rows = c.execute("SELECT id, logical_path, ext FROM files").fetchall()
    overrides = sorted(c.execute("SELECT * FROM overrides").fetchall(), key=lambda r: len(r["prefix"]))
    by_folder: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        lp = PurePosixPath(r["logical_path"])
        by_folder[str(lp.parent)].append(lp.stem)
    release_creators = {r["release"].lower(): r["creator"]
                        for r in c.execute("SELECT release, creator FROM release_creators")}
    layouts, default_depth = folder_layouts(), settings.get("release_depth")
    updates = []
    for r in rows:
        lp = r["logical_path"]
        g = classify.guess(lp, depth_for(lp, layouts, default_depth), by_folder[str(PurePosixPath(lp).parent)])
        release, model, option, supported, hidden = g.release, g.model, g.option, g.supported, 0
        creator = g.creator
        for o in overrides:
            pre = o["prefix"]
            if lp == pre or lp.startswith(pre.rstrip("/") + "/"):
                release = o["release"] if o["release"] is not None else release
                model = o["model"] if o["model"] is not None else model
                option = o["option"] if o["option"] is not None else option
                creator = o["creator"] if o["creator"] is not None else creator
                if o["supported"] is not None:  # 1 supported, 0 unsupported, -1 unknown
                    supported = None if o["supported"] == -1 else bool(o["supported"])
                hidden = o["hidden"] if o["hidden"] is not None else hidden
        creator = release_creators.get(release.lower(), creator)
        sup = None if supported is None else int(supported)
        updates.append([creator, release, model, model_id(release, model), option, sup,
                        g.model_root, hidden, r["id"]])
    # A file with no support marker next to "<same name> supported" is the unsupported copy.
    stems = {r["id"]: classify.strip_tokens(PurePosixPath(r["logical_path"]).stem) for r in rows}
    mesh = {r["id"] for r in rows if r["ext"] in (".stl", ".obj", ".3mf")}
    supported_names = defaultdict(set)
    for u in updates:
        if u[5] == 1:
            supported_names[u[3]].add(stems[u[8]])
    for u in updates:
        if u[5] is None and u[8] in mesh and stems[u[8]] and stems[u[8]] in supported_names[u[3]]:
            u[5] = 0
    c.executemany("UPDATE files SET creator=?, release=?, model=?, model_id=?, option=?, supported=?, "
                  "model_root=?, hidden=? WHERE id=?", updates)
    match_images(c, overrides)
    c.commit()


# ---------------------------------------------------------------- bundled preview pictures

# Words that describe the picture rather than the model ("Knight_render_2.jpg").
_IMAGE_WORDS = {
    "preview", "previews", "render", "renders", "rendered", "image", "images", "img", "pic", "pics",
    "picture", "pictures", "photo", "photos", "cover", "thumb", "thumbnail", "thumbnails", "promo",
    "painted", "unpainted", "view", "front", "back", "side", "main", "hero", "showcase", "gallery",
}
# Pictures named like this are picked as the cover first.
_COVER_WORDS = {"cover", "main", "hero", "promo", "preview", "thumbnail", "thumb", "showcase"}


def _name_key(stem: str) -> str:
    words = [w for w in classify.strip_tokens(stem).split() if w not in _IMAGE_WORDS]
    while words and words[-1].isdigit():
        words.pop()
    return " ".join(words)


def _name_match(key: str, candidates: dict) -> tuple | None:
    """The model whose name the picture's name is, or starts with (longest wins)."""
    if not key:
        return None
    best = None
    for k, info in candidates.items():
        if (key == k or key.startswith(k + " ")) and (best is None or len(k) > len(best[0])):
            best = (k, info)
    return best[1] if best else None


def match_images(c, overrides):
    """Attach each picture to a model, or else to its release.

    * A picture inside a model's folder (at any depth) belongs to that model.
    * Otherwise a picture named after a model belongs to it: first models whose
      files sit loose in the same folder, then any model of the release
      (``Release/Renders/Knight_front.jpg`` -> Knight).
    * Anything else is a release picture. When the release has a single model,
      release pictures also count as that model's pictures.
    """
    files = c.execute("SELECT logical_path, model, model_id, model_root, release, hidden FROM files").fetchall()
    roots: dict[str, tuple] = {}
    loose: dict[str, dict] = defaultdict(dict)
    by_release: dict[str, dict] = defaultdict(dict)
    release_models: dict[str, set] = defaultdict(set)
    for f in files:
        info = (f["model_id"], f["release"], f["hidden"])
        lp = PurePosixPath(f["logical_path"])
        if f["model_root"] != f["logical_path"]:
            roots.setdefault(f["model_root"], info)
        else:
            for k in (classify.strip_tokens(f["model"]), _name_key(lp.stem)):
                if k:
                    loose[str(lp.parent)].setdefault(k, info)
        k = classify.strip_tokens(f["model"])
        if k:
            by_release[f["release"]].setdefault(k, info)
        if not f["hidden"]:
            release_models[f["release"]].add(f["model_id"])

    updates = []
    layouts, default_depth = folder_layouts(), settings.get("release_depth")
    for r in c.execute("SELECT id, logical_path FROM images").fetchall():
        lp = PurePosixPath(r["logical_path"])
        folders = list(lp.parent.parts)
        key = _name_key(lp.stem)
        release = classify.guess(r["logical_path"], depth_for(r["logical_path"], layouts, default_depth)).release
        hidden = 0
        for o in overrides:
            pre = o["prefix"]
            if r["logical_path"] == pre or r["logical_path"].startswith(pre.rstrip("/") + "/"):
                release = o["release"] if o["release"] is not None else release
                hidden = o["hidden"] if o["hidden"] is not None else hidden
        info = next((roots[p] for p in ("/".join(folders[:i]) for i in range(len(folders), 0, -1)) if p in roots),
                    None)
        info = info or _name_match(key, loose.get(str(lp.parent), {})) or _name_match(key, by_release.get(release, {}))
        cover = bool(_COVER_WORDS & set(classify.norm(lp.stem).split()))
        rank = (0 if cover else 100) + len(folders)
        if info:
            mid, release, model_hidden = info
            updates.append((release, mid, "model", rank, int(hidden or model_hidden), r["id"]))
        else:
            only = release_models.get(release, set())
            mid = next(iter(only)) if len(only) == 1 else ""
            updates.append((release, mid, "release", rank, hidden, r["id"]))
    c.executemany("UPDATE images SET release=?, model_id=?, scope=?, rank=?, hidden=? WHERE id=?", updates)


def _ids_in_chunks(q: str, ids: list[str]):
    c = db.conn()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        yield from c.execute(q % ",".join("?" * len(chunk)), chunk)


def cover_image_ids(model_ids: list[str]) -> dict[str, int]:
    """Best bundled picture per model: its own pictures before release ones."""
    out: dict[str, int] = {}
    q = ("SELECT model_id, id FROM images WHERE hidden=0 AND preview != 'error' AND model_id IN (%s) "
         "ORDER BY model_id, scope != 'model', rank, name COLLATE NOCASE")
    for r in _ids_in_chunks(q, model_ids):
        out.setdefault(r["model_id"], r["id"])
    return out


def _image_dict(r):
    return {"id": r["id"], "name": r["name"], "path": r["logical_path"], "scope": r["scope"],
            "archive": r["rel_path"] if r["member"] else None, "size": r["size"]}


def model_images(model_id: str) -> list[dict]:
    rows = db.conn().execute("SELECT * FROM images WHERE model_id=? AND hidden=0 AND preview != 'error' "
                             "ORDER BY scope != 'model', rank, name COLLATE NOCASE", (model_id,)).fetchall()
    return [_image_dict(r) for r in rows]


def release_images(release: str) -> list[dict]:
    rows = db.conn().execute("SELECT * FROM images WHERE release=? AND scope='release' AND hidden=0 "
                             "AND preview != 'error' ORDER BY rank, name COLLATE NOCASE", (release,)).fetchall()
    return [_image_dict(r) for r in rows]


# ---------------------------------------------------------------- previews

def cache_path(row) -> Path:
    key = f"{row['rel_path']}\x00{row['member']}\x00{row['size']}\x00{row['mtime']}\x00{settings.get('preview_size')}"
    h = hashlib.sha1(key.encode()).hexdigest()
    return config.CACHE_DIR / h[:2] / f"{h}.webp"


def _make_preview(row, data: bytes) -> bytes | None:
    if row["ext"] in config.IMAGE_EXTS:
        return render.image_thumbnail(data, settings.get("preview_size"))
    if row["ext"] in config.RENDERABLE_EXTS:
        return render.render_stl_bytes(data, settings.get("preview_size"))
    if row["ext"] in config.THUMBNAIL_EXTS:
        thumb = render.embedded_thumbnail(data, settings.get("preview_size"))
        if thumb is None and data[:2] == b"PK":
            import io, zipfile
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    imgs = [i for i in z.infolist() if i.filename.lower().endswith((".png", ".jpg", ".jpeg"))]
                    if imgs:
                        best = max(imgs, key=lambda i: i.file_size)
                        thumb = render.embedded_thumbnail(z.read(best), settings.get("preview_size"))
            except zipfile.BadZipFile:
                pass
        return thumb
    return None


def _table(row) -> str:
    return "images" if row["ext"] in config.IMAGE_EXTS else "files"


def _store(row, data: bytes | None, error: str | None = None):
    c = db.conn()
    table = _table(row)
    if data:
        p = cache_path(row)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        c.execute(f"UPDATE {table} SET preview='ok', preview_error=NULL WHERE id=?", (row["id"],))
    else:
        c.execute(f"UPDATE {table} SET preview=?, preview_error=? WHERE id=?",
                  ("error" if error else "none", error, row["id"]))
    c.commit()


_render_locks: dict[tuple, threading.Lock] = defaultdict(threading.Lock)
_on_demand = threading.BoundedSemaphore(2)  # cap CPU used by previews rendered on request


def preview_for(file_id: int, table: str = "files") -> Path | None:
    """Return the cached preview of a file or picture, rendering it now if needed."""
    c = db.conn()
    row = c.execute(f"SELECT * FROM {table} WHERE id=?", (file_id,)).fetchone()
    if not row:
        return None
    p = cache_path(row)
    if p.exists():
        return p
    if row["preview"] in ("none", "error"):
        return None
    with _render_locks[(table, file_id)]:
        if p.exists():
            return p
        with _on_demand:
            if not p.exists():
                _render_rows([row])
    return p if p.exists() else None


def _too_big(row) -> bool:
    return row["size"] > settings.get("max_preview_mb") * 1024 * 1024


def _render_rows(rows):
    """Render previews for rows of one table that share one physical file or archive."""
    c = db.conn()
    for r in rows:  # already cached under the same content key (e.g. an archive re-read)
        if r["preview"] == "pending" and cache_path(r).exists():
            c.execute(f"UPDATE {_table(r)} SET preview='ok', preview_error=NULL WHERE id=?", (r["id"],))
    c.commit()
    rows = [r for r in rows if not cache_path(r).exists()]
    for r in [r for r in rows if _too_big(r)]:
        _store(r, None, f"larger than the {settings.get('max_preview_mb')} MB preview limit")
    rows = [r for r in rows if not _too_big(r)]
    if not rows:
        return
    path = config.LIBRARY_DIR / rows[0]["rel_path"]
    try:
        if not rows[0]["member"]:
            for r in rows:
                try:
                    _store(r, _make_preview(r, path.read_bytes()))
                except Exception as e:
                    _store(r, None, str(e))
            return
        by_member = {r["member"]: r for r in rows}
        got = set()
        for member, data in archives.iter_members(path, list(by_member)):
            r = by_member[member]
            got.add(member)
            try:
                _store(r, _make_preview(r, data))
            except Exception as e:
                _store(r, None, str(e))
        for m, r in by_member.items():
            if m not in got:
                _store(r, None, "member not found in archive")
    except Exception as e:
        log.warning("preview failed for %s: %s", path, e)
        for r in rows:
            _store(r, None, str(e))


def cover_file_ids(model_ids: list[str] | None = None) -> dict[str, int]:
    """Pick one file per model for its card: an unsupported STL, biggest first."""
    c = db.conn()
    q = ("SELECT model_id, id FROM files WHERE hidden=0 {where} "
         "ORDER BY model_id, (preview IN ('none','error')), (ext != '.stl'), "
         "COALESCE(supported, 0), size DESC")
    out: dict[str, int] = {}
    if model_ids is None:
        for r in c.execute(q.format(where="")):
            out.setdefault(r["model_id"], r["id"])
        return out
    for i in range(0, len(model_ids), 500):
        chunk = model_ids[i:i + 500]
        for r in c.execute(q.format(where="AND model_id IN (%s)" % ",".join("?" * len(chunk))), chunk):
            out.setdefault(r["model_id"], r["id"])
    return out


preview_state = {"current": None}


def preview_worker(index: int = 0):
    """Background renderer. Model covers first, then every other file.

    MAX_WORKERS of these are started; only the first `preview_workers` do work."""
    while True:
        _preview_wakeup.wait(timeout=60)
        _preview_wakeup.clear()
        if not settings.get("prerender") or index >= settings.get("preview_workers"):
            continue
        try:
            _render_pending()
        except Exception:
            log.exception("preview worker error")
            time.sleep(5)
        preview_state["current"] = None


def _render_pending():
    c = db.conn()
    # Bundled pictures first: they are cheap and become covers.
    for (rel_path,) in c.execute("SELECT DISTINCT rel_path FROM images WHERE preview='pending' AND hidden=0 "
                                 "ORDER BY rel_path").fetchall():
        rows = c.execute("SELECT * FROM images WHERE preview='pending' AND hidden=0 AND rel_path=?",
                         (rel_path,)).fetchall()
        preview_state["current"] = rel_path
        _render_rows(rows)
    cover_ids = set(cover_file_ids().values())
    pending = c.execute("SELECT id, rel_path FROM files WHERE preview='pending' AND hidden=0 "
                        "ORDER BY rel_path").fetchall()
    order: dict[str, bool] = {}
    for r in pending:  # physical files holding a cover go first
        order[r["rel_path"]] = order.get(r["rel_path"], False) or r["id"] in cover_ids
    for rel_path in sorted(order, key=lambda k: not order[k]):
        rows = c.execute("SELECT * FROM files WHERE preview='pending' AND hidden=0 AND rel_path=?",
                         (rel_path,)).fetchall()
        preview_state["current"] = rel_path
        _render_rows(rows)


def apply_settings_change(changed: dict):
    """Catch the library up after settings were changed in the app."""
    c = db.conn()
    for table in ("files", "images"):
        if "preview_size" in changed:
            c.execute(f"UPDATE {table} SET preview='pending' WHERE preview='ok'")
        if "max_preview_mb" in changed:
            c.execute(f"UPDATE {table} SET preview='pending', preview_error=NULL "
                      "WHERE preview='error' AND preview_error LIKE 'larger than%'")
    c.commit()
    if "release_depth" in changed:
        request_reindex()
    _preview_wakeup.set()
