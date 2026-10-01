import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Your existing library. Mounted read-only; only ever read during import.
SOURCE_DIR = Path(os.environ.get("SOURCE_DIR", "/source"))


def _dir(name: str, default: str, legacy: str) -> Path:
    """An env override, else the default mount. Containers still started with
    the old single /storage mount (library/ + data/ inside it) keep using it."""
    if os.environ.get(name):
        return Path(os.environ[name])
    if not os.path.ismount(default) and Path(legacy).is_dir():
        return Path(legacy)
    return Path(default)


# The managed copy the app indexes and serves.
LIBRARY_DIR = _dir("LIBRARY_DIR", "/library", "/storage/library")
# Database, settings and preview image cache.
DATA_DIR = _dir("DATA_DIR", "/data", "/storage/data")

# How many folder levels sit above the release folder (e.g. 1 for Creator/Release/...).
RELEASE_DEPTH = _int("RELEASE_DEPTH", 0)
PREVIEW_SIZE = _int("PREVIEW_SIZE", 512)
PREVIEW_WORKERS = max(1, _int("PREVIEW_WORKERS", 1))
# Render previews for every STL in the background (otherwise only on demand).
PRERENDER = os.environ.get("PRERENDER", "1") not in ("0", "false", "no")
# Skip previews for STLs larger than this many MB (protects small NAS boxes).
MAX_PREVIEW_MB = _int("MAX_PREVIEW_MB", 1024)

# Short git commit baked in at image build (docker build --build-arg GIT_COMMIT=...).
GIT_COMMIT = os.environ.get("GIT_COMMIT", "").strip()[:7]

MODEL_EXTS = {".stl", ".lys", ".ctx", ".ctb", ".chitubox", ".obj", ".3mf"}
ARCHIVE_EXTS = {".zip", ".7z"}
RENDERABLE_EXTS = {".stl"}
THUMBNAIL_EXTS = {".lys", ".ctx", ".ctb", ".chitubox", ".3mf"}
# Preview pictures shipped with releases and models.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

DB_PATH = DATA_DIR / "library.db"
CACHE_DIR = DATA_DIR / "previews"
TMP_DIR = DATA_DIR / "tmp"
