import sqlite3
import threading

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    rel_path TEXT NOT NULL,          -- path of the file on disk, relative to LIBRARY_DIR
    member TEXT NOT NULL DEFAULT '', -- path inside the archive, '' for loose files
    logical_path TEXT NOT NULL,      -- archive treated as a folder
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    creator TEXT NOT NULL DEFAULT '',
    release TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    option TEXT NOT NULL DEFAULT '',
    supported INTEGER,               -- 1 supported, 0 unsupported, NULL unknown
    model_root TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT 'pending', -- pending | ok | none | error
    preview_error TEXT,
    seen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(rel_path, member)
);
CREATE INDEX IF NOT EXISTS files_model ON files(model_id);
CREATE INDEX IF NOT EXISTS files_release ON files(release);
CREATE INDEX IF NOT EXISTS files_preview ON files(preview);

-- Preview pictures that came with the files (loose or inside archives).
-- Matched to a model (model_id set) or only to a release on every reclassify.
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    rel_path TEXT NOT NULL,
    member TEXT NOT NULL DEFAULT '',
    logical_path TEXT NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    release TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',  -- '' for release-level pictures
    scope TEXT NOT NULL DEFAULT 'release', -- model | release
    rank INTEGER NOT NULL DEFAULT 0,    -- lower is a better cover
    hidden INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT 'pending',
    preview_error TEXT,
    seen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(rel_path, member)
);
CREATE INDEX IF NOT EXISTS images_model ON images(model_id);
CREATE INDEX IF NOT EXISTS images_release ON images(release);

CREATE TABLE IF NOT EXISTS archives (
    rel_path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    error TEXT,
    listed INTEGER NOT NULL DEFAULT 1  -- library.ARCHIVE_LISTING when the members were last read
);

-- Tags belong to a model (release + model name), not to files, so they survive rescans.
CREATE TABLE IF NOT EXISTS model_tags (
    model_id TEXT NOT NULL,
    tag TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (model_id, tag)
);
CREATE INDEX IF NOT EXISTS model_tags_tag ON model_tags(tag);

-- Picture chosen in the app as the main picture of a release (key = release
-- name) or of a model (key = model_id). Stored by the picture's logical path so
-- it survives re-indexing; if the picture disappears the automatic pick is used.
CREATE TABLE IF NOT EXISTS main_pictures (
    kind TEXT NOT NULL,              -- release | model
    key TEXT NOT NULL COLLATE NOCASE,
    path TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

-- Creator set for a release from the app. Wins over the folder guess and import
-- rules, and survives re-indexing because it is keyed on the release name.
CREATE TABLE IF NOT EXISTS release_creators (
    release TEXT PRIMARY KEY COLLATE NOCASE,
    creator TEXT NOT NULL
);

-- Layout of each top-level library folder, recorded when it arrives: how many
-- folder levels sit above the release (0 Release/Model, 1 Creator/Release/Model).
-- Folders without a row (e.g. a copied NAS library) use the release_depth setting.
CREATE TABLE IF NOT EXISTS folder_layouts (
    folder TEXT PRIMARY KEY,         -- first part of the logical path
    depth INTEGER NOT NULL
);

-- Folder mappings the user drew on a path: what each folder level is (creator,
-- release, model, ignore) for every file under prefix. Longer prefixes win, and a
-- mapping replaces the automatic guess; corrections (overrides) still apply on top.
CREATE TABLE IF NOT EXISTS path_maps (
    id INTEGER PRIMARY KEY,
    prefix TEXT NOT NULL UNIQUE,
    roles TEXT NOT NULL,             -- JSON list, one role per folder from the library root
    created REAL DEFAULT (strftime('%s','now'))
);

-- The user's MyMiniFactory library (purchases, pledges, tribes), sent by the sync
-- bookmarklet from their logged-in browser. Only listed here; nothing is downloaded.
CREATE TABLE IF NOT EXISTS mmf_items (
    id INTEGER PRIMARY KEY,          -- MyMiniFactory object id
    name TEXT NOT NULL,
    creator TEXT NOT NULL DEFAULT '',
    creator_url TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL,
    images TEXT NOT NULL DEFAULT '[]',     -- JSON list of picture URLs
    downloads TEXT NOT NULL DEFAULT '[]',  -- JSON list of {url, name} the bookmarklet can fetch
    queued INTEGER NOT NULL DEFAULT 0,     -- 1 = download into the library on the next sync
    download_note TEXT NOT NULL DEFAULT '' -- result of the last download attempt
);
-- Where each item is in the MyMiniFactory library: source purchase | pledge | tribe,
-- collection the campaign or tribe name ('' for plain purchases).
CREATE TABLE IF NOT EXISTS mmf_links (
    item_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    collection TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (item_id, source, collection)
);

-- Models combined into one: every model of the release named in parts becomes an
-- option group (named after it) of the model called name. Keyed on names, like
-- release_creators, so it survives re-indexing; deleting the row splits them again.
CREATE TABLE IF NOT EXISTS model_combines (
    id INTEGER PRIMARY KEY,
    release TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    parts TEXT NOT NULL,             -- JSON list of the former model names
    created REAL DEFAULT (strftime('%s','now'))
);

-- Values saved from the Settings dialog (see settings.py).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- User corrections. Applied to every file whose logical path starts with prefix;
-- longer prefixes win. NULL columns leave the heuristic value alone.
CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY,
    prefix TEXT NOT NULL UNIQUE,
    release TEXT,
    model TEXT,
    option TEXT,
    creator TEXT,
    supported INTEGER,
    hidden INTEGER,
    created REAL DEFAULT (strftime('%s','now'))
);
"""

_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=60)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    if "listed" not in {r["name"] for r in c.execute("PRAGMA table_info(archives)")}:
        c.execute("ALTER TABLE archives ADD COLUMN listed INTEGER NOT NULL DEFAULT 1")
    if "creator" not in {r["name"] for r in c.execute("PRAGMA table_info(overrides)")}:
        c.execute("ALTER TABLE overrides ADD COLUMN creator TEXT")
    mmf_cols = {r["name"] for r in c.execute("PRAGMA table_info(mmf_items)")}
    for col, decl in (("images", "TEXT NOT NULL DEFAULT '[]'"), ("downloads", "TEXT NOT NULL DEFAULT '[]'"),
                      ("queued", "INTEGER NOT NULL DEFAULT 0"), ("download_note", "TEXT NOT NULL DEFAULT ''")):
        if col not in mmf_cols:
            c.execute(f"ALTER TABLE mmf_items ADD COLUMN {col} {decl}")
    c.commit()
