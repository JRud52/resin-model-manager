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
# Database, settings and preview image cache.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

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

# Uploads by users other than the main admin go into LIBRARY_DIR/<USERS_FOLDER>/<username>/.
USERS_FOLDER = "_users"

# Sign-in through a reverse proxy with forward auth (Authelia, Authentik, ...): the proxy
# checks who you are and passes the username on in a header. Off unless AUTH_PROXY is set.
AUTH_PROXY = os.environ.get("AUTH_PROXY", "0").strip().lower() in ("1", "true", "yes", "on")
# Who may send those headers: IPs, networks (10.0.0.0/8) or host names (a proxy container's
# name on a shared Docker network), comma separated. Headers from anywhere else are ignored.
AUTH_PROXY_TRUSTED = [t.strip() for t in os.environ.get("AUTH_PROXY_TRUSTED", "").split(",") if t.strip()]
AUTH_PROXY_USER_HEADER = os.environ.get("AUTH_PROXY_USER_HEADER", "Remote-User").strip()
AUTH_PROXY_GROUPS_HEADER = os.environ.get("AUTH_PROXY_GROUPS_HEADER", "Remote-Groups").strip()
# Members of this group are admins (and others aren't); empty leaves admin rights to the app.
AUTH_PROXY_ADMIN_GROUP = os.environ.get("AUTH_PROXY_ADMIN_GROUP", "").strip()
# Where Log out goes for people signed in through the proxy (e.g. https://auth.example.com/logout).
AUTH_PROXY_LOGOUT_URL = os.environ.get("AUTH_PROXY_LOGOUT_URL", "").strip()
