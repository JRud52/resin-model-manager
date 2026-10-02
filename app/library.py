"""Import, scanning, classification and preview rendering."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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
        raise ValueError(f"{parts[-1]}: not a model file, image or .zip/.7z archive")
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

def model_key(model: str) -> str:
    """Model names that differ only in support/format words are the same model."""
    return classify.strip_tokens(model) or classify.norm(model)


def model_id(release: str, model: str) -> str:
    key = classify.norm(release) + "\x00" + model_key(model)
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def path_maps() -> list[dict]:
    return [{"id": r["id"], "prefix": r["prefix"], "roles": json.loads(r["roles"])}
            for r in db.conn().execute("SELECT * FROM path_maps ORDER BY prefix COLLATE NOCASE")]


class _Reader:
    """Reads release / model / creator for logical paths: the user's folder mapping
    for the path when there is one, else the automatic guess."""

    def __init__(self, rows, overrides, set_creators, extra_map: tuple[str, tuple] | None = None):
        self.layouts, self.default_depth = folder_layouts(), settings.get("release_depth")
        self.ignored = classify.parse_keys(settings.get("ignored_folders"))
        maps = {m["prefix"]: tuple(m["roles"]) for m in path_maps()}
        if extra_map:
            maps[extra_map[0]] = tuple(extra_map[1])
        self.maps = sorted(maps.items(), key=lambda kv: -len(kv[0]))  # longest prefix wins
        self.by_folder: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            lp = PurePosixPath(r["logical_path"])
            self.by_folder[str(lp.parent)].append(lp.stem)
        self.creator_keys = self._creator_keys(rows, set_creators, overrides)
        # (release, model key) of every combined part -> (combined name, option group)
        self.combined: dict[tuple[str, str], tuple[str, str]] = {}
        for cb in combines():
            for part in cb["parts"]:
                self.combined[(cb["release"].lower(), model_key(part))] = (cb["name"], part)

    def roles_for(self, lp: str) -> tuple | None:
        return next((roles for pre, roles in self.maps if _under(lp, pre)), None)

    def guess(self, lp: str) -> classify.Guess:
        siblings = self.by_folder.get(str(PurePosixPath(lp).parent), [])
        roles = self.roles_for(lp)
        if roles:
            return classify.mapped(lp, roles, siblings, self.creator_keys)
        return classify.guess(lp, depth_for(lp, self.layouts, self.default_depth), siblings,
                              self.creator_keys, self.ignored)

    def _creator_keys(self, rows, set_creators, overrides) -> set[str]:
        """Every creator name we know of, so a release that repeats its creator's folder
        inside it (Creator/Release/Creator/Model) still finds the real model folders, and
        Freebies/Creator/Release is told apart from Freebies/Release."""
        keys = {classify.name_key(n) for n in set_creators}
        keys |= {classify.name_key(o["creator"]) for o in overrides if o["creator"]}
        for r in rows:
            parts = PurePosixPath(r["logical_path"]).parts
            roles = self.roles_for(r["logical_path"])
            if roles:
                keys |= {classify.name_key(f) for f, role in zip(parts[:-1], roles) if role == "creator"}
                continue
            if parts and classify.name_key(parts[0]) in self.ignored:
                continue
            depth = depth_for(r["logical_path"], self.layouts, self.default_depth)
            keys |= {classify.name_key(f) for f in parts[:min(depth, len(parts) - 1)]}
        return keys - self.ignored - {""}


def _context(c, extra_map=None):
    rows = c.execute("SELECT id, logical_path, ext FROM files").fetchall()
    overrides = sorted(c.execute("SELECT * FROM overrides").fetchall(), key=lambda r: len(r["prefix"]))
    release_creators = {r["release"].lower(): r["creator"]
                        for r in c.execute("SELECT release, creator FROM release_creators")}
    return rows, overrides, release_creators, _Reader(rows, overrides, release_creators.values(), extra_map)


def _classify(rows, overrides, release_creators, reader) -> list[list]:
    """[creator, release, model, model_id, option, supported, model_root, hidden, id] per row."""
    updates = []
    for r in rows:
        lp = r["logical_path"]
        g = reader.guess(lp)
        release, model, option, supported, hidden = g.release, g.model, g.option, g.supported, 0
        creator, set_unknown = g.creator, False
        for o in overrides:
            if _under(lp, o["prefix"]):
                release = o["release"] if o["release"] is not None else release
                model = o["model"] if o["model"] is not None else model
                option = o["option"] if o["option"] is not None else option
                creator = o["creator"] if o["creator"] is not None else creator
                if o["supported"] is not None:  # 1 supported, 0 unsupported, -1 unknown
                    supported = None if o["supported"] == -1 else bool(o["supported"])
                    set_unknown = o["supported"] == -1
                hidden = o["hidden"] if o["hidden"] is not None else hidden
        creator = release_creators.get(release.lower(), creator)
        combined = reader.combined.get((release.lower(), model_key(model)))
        if combined:  # the former model becomes an option group of the combined one
            model, option = combined[0], " / ".join(filter(None, (combined[1], option)))
        sup = None if supported is None else int(supported)
        # Nothing in the name or a parent folder says supported: treat it as unsupported,
        # unless the user explicitly marked it unknown.
        if sup is None and not set_unknown:
            sup = 0
        updates.append([creator, release, model, model_id(release, model), option, sup,
                        g.model_root, hidden, r["id"]])
    return updates


def reclassify():
    """Re-run the heuristics, folder mappings and user overrides over every indexed file."""
    c = db.conn()
    rows, overrides, release_creators, reader = _context(c)
    updates = _classify(rows, overrides, release_creators, reader)
    c.executemany("UPDATE files SET creator=?, release=?, model=?, model_id=?, option=?, supported=?, "
                  "model_root=?, hidden=? WHERE id=?", updates)
    match_images(c, overrides, reader)
    c.commit()


# ---------------------------------------------------------------- folder mappings

def map_suggestion(path: str) -> dict:
    """How a path is read now, as a folder mapping the user can start from."""
    c = db.conn()
    _, _, _, reader = _context(c)
    pre = next((p for p, _ in reader.maps if _under(path, p)), None)
    g = reader.guess(path)
    return {"roles": list(g.roles), "map": next((m for m in path_maps() if m["prefix"] == pre), None)}


def _groups(updates) -> list[dict]:
    out: dict[tuple, dict] = {}
    for u in updates:
        if u[7]:  # hidden
            continue
        g = out.setdefault(u[3], {"creator": u[0], "release": u[1], "model": u[2], "files": 0})
        g["files"] += 1
    return sorted(out.values(), key=lambda g: (g["creator"].lower(), g["release"].lower(), g["model"].lower()))


def preview_map(prefix: str, roles: tuple) -> dict:
    """How the files under prefix group now, and how they would with this mapping."""
    c = db.conn()
    rows, overrides, release_creators, reader = _context(c, (prefix, roles))
    affected = [r for r in rows if _under(r["logical_path"], prefix)]
    now = c.execute("SELECT creator, release, model, model_id, hidden, logical_path FROM files").fetchall()
    before = [[r["creator"], r["release"], r["model"], r["model_id"], 0, 0, "", r["hidden"]]
              for r in now if _under(r["logical_path"], prefix)]
    after = _classify(affected, overrides, release_creators, reader)
    return {"files": len(affected), "before": _groups(before), "after": _groups(after)}


def _model_ids(prefix: str) -> dict[int, str]:
    return {r["id"]: r["model_id"] for r in db.conn().execute("SELECT id, model_id, logical_path FROM files")
            if _under(r["logical_path"], prefix)}


def _carry_tags(before: dict[int, str]):
    """Tags follow files whose model changed (copied, so undoing a mapping keeps them)."""
    c = db.conn()
    after = {r["id"]: r["model_id"] for r in c.execute("SELECT id, model_id FROM files")}
    for old, new in {(m, after[i]) for i, m in before.items() if i in after and after[i] != m}:
        c.execute("INSERT OR IGNORE INTO model_tags(model_id, tag) SELECT ?, tag FROM model_tags WHERE model_id=?",
                  (new, old))
    c.commit()


def save_map(prefix: str, roles: tuple):
    c = db.conn()
    before = _model_ids(prefix)
    c.execute("INSERT INTO path_maps(prefix, roles) VALUES (?, ?) ON CONFLICT(prefix) DO UPDATE SET roles=excluded.roles",
              (prefix, json.dumps(list(roles))))
    c.commit()
    reclassify()
    _carry_tags(before)
    _preview_wakeup.set()


def delete_map(map_id: int) -> bool:
    c = db.conn()
    row = c.execute("SELECT prefix FROM path_maps WHERE id=?", (map_id,)).fetchone()
    if not row:
        return False
    before = _model_ids(row["prefix"])
    c.execute("DELETE FROM path_maps WHERE id=?", (map_id,))
    c.commit()
    reclassify()
    _carry_tags(before)
    return True


# ---------------------------------------------------------------- combined models

def combines(release: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM model_combines", ()
    if release is not None:
        q, args = q + " WHERE release = ?", (release,)
    return [{"id": r["id"], "release": r["release"], "name": r["name"], "parts": json.loads(r["parts"])}
            for r in db.conn().execute(q + " ORDER BY release COLLATE NOCASE, name COLLATE NOCASE", args)]


def combine_for(release: str, model: str) -> dict | None:
    """The combine that made this model, if any."""
    return next((cb for cb in combines(release) if model_key(cb["name"]) == model_key(model)), None)


def _release_ids(release: str) -> dict[int, str]:
    return {r["id"]: r["model_id"] for r in db.conn().execute(
        "SELECT id, model_id FROM files WHERE release = ? COLLATE NOCASE", (release,))}


def _regroup(before: dict[int, str]):
    reclassify()
    _carry_tags(before)
    _preview_wakeup.set()


def combine_models(release: str, model_ids: list[str], name: str) -> dict:
    """Combine models of a release into one called name; each becomes an option group
    named after it. A selected model that is itself a combine is merged into the new one."""
    c = db.conn()
    names: dict[str, str] = {}
    for r in c.execute("SELECT model_id, MIN(model) model FROM files WHERE release = ? GROUP BY model_id", (release,)):
        names[r["model_id"]] = r["model"]
    picked = [names[m] for m in dict.fromkeys(model_ids) if m in names]
    if len(picked) < 2:
        raise ValueError("Pick at least two models of the release")
    parts: list[str] = []
    absorbed = []
    for model in picked:
        cb = combine_for(release, model)
        if cb:
            parts += cb["parts"]
            absorbed.append(cb["id"])
        else:
            parts.append(model)
    parts = list({model_key(p): p for p in reversed(parts)}.values())[::-1]  # drop repeats, keep order
    before = _release_ids(release)
    c.executemany("DELETE FROM model_combines WHERE id = ?", [(i,) for i in absorbed])
    c.execute("INSERT INTO model_combines(release, name, parts) VALUES (?, ?, ?)",
              (release, name, json.dumps(parts)))
    c.commit()
    _regroup(before)
    return {"id": model_id(release, name), "parts": parts}


def update_combine(combine_id: int, name: str, release: str) -> bool:
    c = db.conn()
    row = c.execute("SELECT release FROM model_combines WHERE id = ?", (combine_id,)).fetchone()
    if not row:
        return False
    before = {**_release_ids(row["release"]), **_release_ids(release)}
    c.execute("UPDATE model_combines SET name = ?, release = ? WHERE id = ?", (name, release, combine_id))
    c.commit()
    _regroup(before)
    return True


_OVERRIDE_FIELDS = ("release", "model", "option", "creator", "supported", "hidden")


def add_overrides(prefixes: list[str], vals: dict):
    """Save correction rules (without reclassifying); None fields keep what a rule already says."""
    c = db.conn()
    vals = {k: vals.get(k) for k in _OVERRIDE_FIELDS}
    for prefix in prefixes:
        existing = c.execute("SELECT * FROM overrides WHERE prefix=?", (prefix,)).fetchone()
        if existing:
            merged = {k: (vals[k] if vals[k] is not None else existing[k]) for k in vals}
            c.execute("UPDATE overrides SET release=?, model=?, option=?, creator=?, supported=?, hidden=? WHERE id=?",
                      (*merged.values(), existing["id"]))
        else:
            c.execute("INSERT INTO overrides(prefix, release, model, option, creator, supported, hidden) "
                      "VALUES (?,?,?,?,?,?,?)", (prefix, *vals.values()))
    c.commit()


def bulk_edit(model_ids: list[str], release: str | None = None, supported: int | None = None,
              hidden: int | None = None) -> list[str]:
    """Move several models to a release, set their support or hide them, as correction rules
    on their folders. Tags and combines move with them. Returns the releases they end up in."""
    c = db.conn()
    rows = list(_ids_in_chunks("SELECT id, model_id, model_root, release, model FROM files WHERE model_id IN (%s)",
                               model_ids))
    if not rows:
        return []
    before = {r["id"]: r["model_id"] for r in rows}
    if release is not None or supported is not None or hidden is not None:
        add_overrides(sorted({r["model_root"] for r in rows}),
                      {"release": release, "supported": supported, "hidden": hidden})
        if release is not None:  # a combined model keeps its option groups in the new release
            for rel, model in {(r["release"], r["model"]) for r in rows}:
                cb = combine_for(rel, model)
                if cb:
                    c.execute("UPDATE model_combines SET release=? WHERE id=?", (release, cb["id"]))
            c.commit()
        _regroup(before)
    return [release] if release is not None else sorted({r["release"] for r in rows})


def split_combine(combine_id: int) -> bool:
    """Undo a combine: its parts are separate models again (tags are copied back to them)."""
    c = db.conn()
    row = c.execute("SELECT release FROM model_combines WHERE id = ?", (combine_id,)).fetchone()
    if not row:
        return False
    before = _release_ids(row["release"])
    c.execute("DELETE FROM model_combines WHERE id = ?", (combine_id,))
    c.commit()
    _regroup(before)
    return True


# ---------------------------------------------------------------- bundled preview pictures

# Words that describe the picture rather than the model ("Knight_render_2.jpg").
_IMAGE_WORDS = {
    "preview", "previews", "render", "renders", "rendered", "image", "images", "img", "pic", "pics",
    "picture", "pictures", "photo", "photos", "cover", "thumb", "thumbnail", "thumbnails", "promo",
    "painted", "unpainted", "view", "front", "back", "side", "main", "hero", "showcase", "gallery",
}
# Pictures named like this are picked as the cover first.
_COVER_WORDS = {"cover", "main", "hero", "promo", "preview", "thumbnail", "thumb", "showcase"}


def _words(text: str, release: str = "") -> list[str]:
    """Words that name a model: support/scale/format words, picture words like
    "render", the release name and numbering ("01", "v2") are dropped."""
    words = classify.strip_tokens(text, release).split()
    return [w for w in words if w not in _IMAGE_WORDS and not re.fullmatch(r"v?\d+[a-z]?", w)]


def _contains(hay: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def _name_match(words: list[str], candidates: dict) -> tuple | None:
    """The model a picture's name (or folder name) points to.

    The picture's words contain a model's name ("Red_Dragon_front", "DL_01_Red_Dragon";
    longest name wins), or spelled without spaces ("reddragon"). Failing that, the
    picture's words are the start of exactly one model's name ("Goblin" -> Goblin Boss).
    """
    if not words:
        return None
    best = None
    compact = "".join(words)
    for k, info in candidates.items():
        kw = k.split()
        if _contains(words, kw) or (len(k) >= 5 and compact.startswith(k.replace(" ", ""))):
            if best is None or len(k) > len(best[0]):
                best = (k, info)
    if best:
        return best[1]
    if sum(len(w) for w in words) < 3:
        return None
    hits = {info for k, info in candidates.items() if k.split()[:len(words)] == words}
    return hits.pop() if len(hits) == 1 else None


def match_images(c, overrides, reader: _Reader):
    """Attach each picture to a model, or else to its release.

    * A picture inside a model's folder (at any depth) belongs to that model.
    * Otherwise a picture whose name, or a folder it sits in below the release,
      names a model belongs to it: first models whose files sit loose in the same
      folder, then any model of the release (``Release/Renders/Knight_front.jpg``
      or ``Release/Images/Knight/01.jpg`` -> Knight).
    * Anything else is a release picture. When the release has a single model,
      release pictures also count as that model's pictures.
    * A correction rule naming a model (or "" for the release) wins over all of that.
    """
    files = c.execute("SELECT logical_path, model, model_id, model_root, release, hidden FROM files").fetchall()
    roots: dict[str, tuple] = {}
    loose: dict[str, dict] = defaultdict(dict)
    by_release: dict[str, dict] = defaultdict(dict)
    release_models: dict[str, set] = defaultdict(set)
    for f in files:
        info = (f["model_id"], f["release"], f["hidden"])
        lp = PurePosixPath(f["logical_path"])
        keys = [" ".join(_words(f["model"], f["release"]))]
        if f["model_root"] != f["logical_path"]:
            roots.setdefault(f["model_root"], info)
        else:  # loose files are also known by their own names ("Orc_Warboss_Body")
            keys.append(" ".join(_words(lp.stem, f["release"])))
            for k in keys:
                if k:
                    loose[str(lp.parent)].setdefault(k, info)
        for k in keys:
            if k:
                by_release[f["release"]].setdefault(k, info)
        if not f["hidden"]:
            release_models[f["release"]].add(f["model_id"])

    known = {f["model_id"] for f in files}  # a picture rule naming a model that no longer exists is ignored
    updates = []
    for r in c.execute("SELECT id, logical_path FROM images").fetchall():
        lp = PurePosixPath(r["logical_path"])
        folders = list(lp.parent.parts)
        g = reader.guess(r["logical_path"])
        release = g.release
        depth = g.release_index if g.release_index >= 0 else len(folders)  # index of the release folder
        release_raw = folders[depth] if len(folders) > depth else ""
        hidden, forced = 0, None
        for o in overrides:
            pre = o["prefix"]
            if r["logical_path"] == pre or r["logical_path"].startswith(pre.rstrip("/") + "/"):
                release = o["release"] if o["release"] is not None else release
                hidden = o["hidden"] if o["hidden"] is not None else hidden
                forced = o["model"] if o["model"] is not None else forced
        cover = bool(_COVER_WORDS & set(classify.norm(lp.stem).split()))
        rank = (0 if cover else 100) + len(folders)
        if forced is not None and (not forced or model_id(release, forced) in known):
            info = (model_id(release, forced), release, 0) if forced else None
        else:
            info = next((roots[p] for p in ("/".join(folders[:i]) for i in range(len(folders), 0, -1))
                         if p in roots), None)
            names = [_words(lp.stem, release_raw)] + [_words(f, release_raw) for f in reversed(folders[depth + 1:])]
            info = (info or _name_match(names[0], loose.get(str(lp.parent), {}))
                    or next(filter(None, (_name_match(n, by_release.get(release, {})) for n in names)), None))
        if info:
            mid, release, model_hidden = info
            updates.append((release, mid, "model", rank, int(hidden or model_hidden), r["id"]))
        else:
            only = release_models.get(release, set())
            mid = next(iter(only)) if len(only) == 1 and forced != "" else ""
            updates.append((release, mid, "release", rank, hidden, r["id"]))
    c.executemany("UPDATE images SET release=?, model_id=?, scope=?, rank=?, hidden=? WHERE id=?", updates)


def _ids_in_chunks(q: str, ids: list[str]):
    c = db.conn()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        yield from c.execute(q % ",".join("?" * len(chunk)), chunk)


def main_picture_ids(kind: str, keys: list[str]) -> dict[str, int]:
    """Pictures picked in the app as the main picture of a release or model."""
    out: dict[str, int] = {}
    q = ("SELECT m.key, MIN(i.id) id FROM main_pictures m JOIN images i ON i.logical_path = m.path "
         "AND i.hidden=0 AND i.preview != 'error' WHERE m.kind = '%s' AND m.key IN (%%s) GROUP BY m.key" % kind)
    lower = {k.lower(): k for k in keys}
    for r in _ids_in_chunks(q, keys):
        out[lower.get(r["key"].lower(), r["key"])] = r["id"]
    return out


def main_file_ids(model_ids: list[str]) -> dict[str, int]:
    """Files (an option's rendered preview) picked in the app as a model's main image."""
    out: dict[str, int] = {}
    q = ("SELECT m.key, MIN(f.id) id FROM main_pictures m JOIN files f ON f.logical_path = m.path "
         "AND f.hidden=0 AND f.preview NOT IN ('none', 'error') WHERE m.kind = 'model' AND m.key IN (%s) "
         "GROUP BY m.key")
    lower = {k.lower(): k for k in model_ids}
    for r in _ids_in_chunks(q, model_ids):
        out[lower.get(r["key"].lower(), r["key"])] = r["id"]
    return out


def set_main_picture(kind: str, key: str, image_id: int | None, file_id: int | None = None):
    """Pick (or with neither id, un-pick) the main picture of a release or model.
    A model's main picture can also be one of its files' rendered previews."""
    c = db.conn()
    if image_id is None and file_id is None:
        c.execute("DELETE FROM main_pictures WHERE kind=? AND key=?", (kind, key))
    else:
        row = (c.execute("SELECT logical_path FROM images WHERE id=?", (image_id,)).fetchone() if file_id is None
               else c.execute("SELECT logical_path FROM files WHERE id=?", (file_id,)).fetchone())
        if not row:
            raise KeyError(image_id)
        c.execute("INSERT OR REPLACE INTO main_pictures(kind, key, path) VALUES (?,?,?)",
                  (kind, key, row["logical_path"]))
    c.commit()


def cover_image_ids(model_ids: list[str]) -> dict[str, int]:
    """Best bundled picture per model: the one picked in the app, then its own
    pictures before release ones. Models whose main image is a file's preview get none."""
    out: dict[str, int] = main_picture_ids("model", model_ids)
    skip = main_file_ids(model_ids)
    model_ids = [m for m in model_ids if m not in skip]
    q = ("SELECT model_id, id FROM images WHERE hidden=0 AND preview != 'error' AND model_id IN (%s) "
         "ORDER BY model_id, scope != 'model', rank, name COLLATE NOCASE")
    for r in _ids_in_chunks(q, model_ids):
        out.setdefault(r["model_id"], r["id"])
    return out


def _image_dict(r):
    return {"id": r["id"], "name": r["name"], "path": r["logical_path"], "scope": r["scope"],
            "release": r["release"], "model_id": r["model_id"],
            "archive": r["rel_path"] if r["member"] else None, "size": r["size"]}


def model_images(model_id: str, release: str = "") -> list[dict]:
    """A model's pictures, its main picture first. `main_model` / `main_release`
    flag the pictures picked as main for the model and for its release."""
    rows = db.conn().execute("SELECT * FROM images WHERE model_id=? AND hidden=0 AND preview != 'error' "
                             "ORDER BY scope != 'model', rank, name COLLATE NOCASE", (model_id,)).fetchall()
    main_model = main_picture_ids("model", [model_id]).get(model_id)
    main_release = main_picture_ids("release", [release]).get(release) if release else None
    out = [{**_image_dict(r), "main_model": r["id"] == main_model, "main_release": r["id"] == main_release}
           for r in rows]
    return sorted(out, key=lambda d: not d["main_model"])


def release_images(release: str) -> list[dict]:
    """A release's pictures, its main picture first. The main picture may be one
    of its models' pictures, picked from the model window."""
    c = db.conn()
    rows = c.execute("SELECT * FROM images WHERE release=? AND scope='release' AND hidden=0 "
                     "AND preview != 'error' ORDER BY rank, name COLLATE NOCASE", (release,)).fetchall()
    main = main_picture_ids("release", [release]).get(release)
    out = [{**_image_dict(r), "main": r["id"] == main} for r in rows if r["id"] != main]
    if main:
        out.insert(0, {**_image_dict(c.execute("SELECT * FROM images WHERE id=?", (main,)).fetchone()), "main": True})
    return out


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
    """Pick one file per model for its card: the one picked in the app as main
    image, else an unsupported STL, biggest first."""
    c = db.conn()
    q = ("SELECT model_id, id FROM files WHERE hidden=0 {where} "
         "ORDER BY model_id, (preview IN ('none','error')), (ext != '.stl'), "
         "COALESCE(supported, 0), size DESC")
    out: dict[str, int] = {}
    if model_ids is None:
        for r in c.execute(q.format(where="")):
            out.setdefault(r["model_id"], r["id"])
        out.update(_all_main_files(c))
        return out
    for i in range(0, len(model_ids), 500):
        chunk = model_ids[i:i + 500]
        for r in c.execute(q.format(where="AND model_id IN (%s)" % ",".join("?" * len(chunk))), chunk):
            out.setdefault(r["model_id"], r["id"])
    out.update(main_file_ids(model_ids))
    return out


def _all_main_files(c) -> dict[str, int]:
    return {r["key"]: r["id"] for r in c.execute(
        "SELECT m.key, MIN(f.id) id FROM main_pictures m JOIN files f ON f.logical_path = m.path "
        "AND f.hidden=0 WHERE m.kind = 'model' GROUP BY m.key")}


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
    if "release_depth" in changed or "ignored_folders" in changed:
        request_reindex()
    _preview_wakeup.set()
