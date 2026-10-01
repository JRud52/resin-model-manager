import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Your existing library. Mounted read-only; only ever read during import.
SOURCE_DIR = Path(os.environ.get("SOURCE_DIR", "/source"))
# The managed copy the app indexes and serves.
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/library"))
# Database and preview image cache.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

# How many folder levels sit above the release folder (e.g. 1 for Creator/Release/...).
RELEASE_DEPTH = _int("RELEASE_DEPTH", 0)
PREVIEW_SIZE = _int("PREVIEW_SIZE", 512)
PREVIEW_WORKERS = max(1, _int("PREVIEW_WORKERS", 1))
# Render previews for every STL in the background (otherwise only on demand).
PRERENDER = os.environ.get("PRERENDER", "1") not in ("0", "false", "no")
# Automatically rescan the library every N minutes (0 = off).
SCAN_INTERVAL_MINUTES = _int("SCAN_INTERVAL_MINUTES", 0)
# Skip previews for STLs larger than this many MB (protects small NAS boxes).
MAX_PREVIEW_MB = _int("MAX_PREVIEW_MB", 1024)

MODEL_EXTS = {".stl", ".lys", ".ctx", ".ctb", ".chitubox", ".obj", ".3mf"}
ARCHIVE_EXTS = {".zip", ".7z"}
RENDERABLE_EXTS = {".stl"}
THUMBNAIL_EXTS = {".lys", ".ctx", ".ctb", ".chitubox", ".3mf"}

DB_PATH = DATA_DIR / "library.db"
CACHE_DIR = DATA_DIR / "previews"
TMP_DIR = DATA_DIR / "tmp"
