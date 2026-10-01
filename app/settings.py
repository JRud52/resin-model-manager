"""Settings changed from the app's Settings dialog, stored in the database.

The matching environment variables (RELEASE_DEPTH, PREVIEW_WORKERS, ...) still
work and act as the defaults until a value is saved in the app.
"""
import threading

from . import config, db

MAX_WORKERS = 8

# key: (default, minimum, maximum)
FIELDS = {
    "release_depth": (config.RELEASE_DEPTH, 0, 5),
    "preview_workers": (min(config.PREVIEW_WORKERS, MAX_WORKERS), 1, MAX_WORKERS),
    "prerender": (int(config.PRERENDER), 0, 1),
    "preview_size": (config.PREVIEW_SIZE, 128, 2048),
    "scan_interval_minutes": (config.SCAN_INTERVAL_MINUTES, 0, 7 * 24 * 60),
    "max_preview_mb": (config.MAX_PREVIEW_MB, 1, 100_000),
}

_lock = threading.Lock()
_values: dict[str, int] | None = None


def values() -> dict[str, int]:
    global _values
    with _lock:
        if _values is None:
            stored = {r["key"]: r["value"] for r in db.conn().execute("SELECT key, value FROM settings")}
            _values = {}
            for k, (default, lo, hi) in FIELDS.items():
                try:
                    _values[k] = min(hi, max(lo, int(stored.get(k, default))))
                except ValueError:
                    _values[k] = default
        return dict(_values)


def get(key: str) -> int:
    return values()[key]


def update(changes: dict) -> dict[str, int]:
    """Validate and save changes; returns only the keys whose value changed."""
    global _values
    current = values()
    changed = {}
    for k, v in changes.items():
        if k not in FIELDS:
            raise ValueError(f"unknown setting {k}")
        _, lo, hi = FIELDS[k]
        v = int(v)
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
        if v != current[k]:
            changed[k] = v
    if changed:
        c = db.conn()
        c.executemany("INSERT INTO settings(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      [(k, str(v)) for k, v in changed.items()])
        c.commit()
        with _lock:
            _values = None
    return changed
