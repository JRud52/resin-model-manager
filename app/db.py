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

CREATE TABLE IF NOT EXISTS archives (
    rel_path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    error TEXT
);

-- Tags belong to a model (release + model name), not to files, so they survive rescans.
CREATE TABLE IF NOT EXISTS model_tags (
    model_id TEXT NOT NULL,
    tag TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (model_id, tag)
);
CREATE INDEX IF NOT EXISTS model_tags_tag ON model_tags(tag);

-- User corrections. Applied to every file whose logical path starts with prefix;
-- longer prefixes win. NULL columns leave the heuristic value alone.
CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY,
    prefix TEXT NOT NULL UNIQUE,
    release TEXT,
    model TEXT,
    option TEXT,
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
    conn().executescript(SCHEMA)
    conn().commit()
