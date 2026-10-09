"""The user's MyMiniFactory library, listed next to the local one.

MyMiniFactory has no API for a user's purchases, pledges or tribes, so the sync
bookmarklet (static/mmf-bookmarklet.js) reads them from the website's own library
page in the user's logged-in browser and posts them here. No MyMiniFactory login
is ever stored by the app. Items marked for download are fetched by the same
bookmarklet (it has the login) on its next run and uploaded into the library.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata

from . import autotag, classify, db

SOURCES = ("purchase", "pledge", "tribe", "group", "mmfplus", "free")
MAX_ITEMS = 50_000


def _text(v, limit: int = 300) -> str:
    return " ".join(str(v or "").split())[:limit]


def _url(v) -> str:
    """Only plain https links are kept (they end up in href and src attributes)."""
    v = str(v or "").strip()
    if v.startswith("//"):
        v = "https:" + v
    elif v.startswith("/"):
        v = "https://www.myminifactory.com" + v
    return v[:1000] if v.startswith("https://") and not any(c in v for c in "\"'<> \n\r\t") else ""


def _tags(v) -> list[str]:
    """Tags as the site sends them: a list of names or of {name: ...}, or one comma-separated string."""
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list):
        return []
    out, seen = [], set()
    for t in v[:200]:
        if isinstance(t, dict):
            t = next((t[k] for k in ("name", "label", "tag", "title", "slug") if isinstance(t.get(k), str) and t[k].strip()), "")
        if not isinstance(t, str):
            continue
        t = " ".join(t.split())[:60]
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out[:50]


def has_tags(item_tags: list[str], wanted: list[str]) -> bool:
    have = {t.casefold() for t in item_tags}
    return all(t.casefold() in have for t in wanted)


def import_library(payload: dict) -> dict:
    """Store a sync. payload = {"items": [...], "complete": ["purchase", ...]}.

    Items of a source listed in `complete` that are missing from this sync are
    dropped; sources that failed part way keep what earlier syncs found."""
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    if len(items) > MAX_ITEMS:
        raise ValueError("too many items")
    complete = [s for s in payload.get("complete") or [] if s in SOURCES]
    rows, links = {}, set()
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            oid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        name = _text(it.get("name"))
        source = it.get("source")
        if oid <= 0 or not name or source not in SOURCES:
            continue
        old = rows.get(oid, {})
        images = [u for u in (_url(x) for x in (it.get("images") or [])[:60] if isinstance(x, str)) if u]
        image = _url(it.get("image")) or old.get("image", "") or (images[0] if images else "")
        if image and image not in images:
            images.insert(0, image)
        downloads = []
        for d in (it.get("downloads") or [])[:500]:
            u = _url(d.get("url")) if isinstance(d, dict) else ""
            if u:
                downloads.append({"url": u, "name": _text(d.get("name"), 200)})
        rows[oid] = {
            "images": json.dumps(images or json.loads(old.get("images", "[]"))),
            "downloads": json.dumps(downloads or json.loads(old.get("downloads", "[]"))),
            "name": name,
            "creator": _text(it.get("creator"), 200) or old.get("creator", ""),
            "creator_url": _url(it.get("creator_url")) or old.get("creator_url", ""),
            "url": _url(it.get("url")) or old.get("url", "") or f"https://www.myminifactory.com/object/{oid}",
            "image": image,
            "tags": json.dumps(_tags(it.get("tags")) or json.loads(old.get("tags", "[]"))),
        }
        links.add((oid, source, _text(it.get("collection"), 300)))
    now = time.time()
    c = db.conn()
    with c:
        for s in complete:
            c.execute("DELETE FROM mmf_links WHERE source=?", (s,))
        c.executemany("""INSERT INTO mmf_items(id, name, creator, creator_url, url, image, images, downloads, tags, updated)
                         VALUES (:id, :name, :creator, :creator_url, :url, :image, :images, :downloads, :tags, :updated)
                         ON CONFLICT(id) DO UPDATE SET name=excluded.name, creator=excluded.creator,
                         creator_url=excluded.creator_url, url=excluded.url, image=excluded.image,
                         images=excluded.images, downloads=excluded.downloads, tags=excluded.tags,
                         updated=excluded.updated""",
                      [{"id": k, **v, "updated": now} for k, v in rows.items()])
        c.executemany("INSERT OR IGNORE INTO mmf_links(item_id, source, collection) VALUES (?,?,?)", sorted(links))
        c.execute("DELETE FROM mmf_items WHERE id NOT IN (SELECT item_id FROM mmf_links)")
        c.execute("INSERT INTO settings(key, value) VALUES('mmf_synced', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
        tagged = _autotag(c, links)
    return {"items": len(rows), "autotagged": tagged, **status()}


def _autotag(c, links: set) -> int:
    """Best-guess tags (autotag.py) on items no sync has tagged before, as tags of their own that
    the user can remove. The genre and race hints come from the tags the creator's other items
    and local models, and the release's other items, already have. Returns the items tagged."""
    new = [r for r in c.execute("SELECT id, name, creator, tags FROM mmf_items WHERE id NOT IN "
                                "(SELECT item_id FROM mmf_autotagged)")]
    if not new:
        return 0
    new_ids = {r["id"] for r in new}
    names = creator_names()
    own = own_tags()
    by_creator: dict[str, list[list[str]]] = {}
    by_collection: dict[str, list[list[str]]] = {}
    collections: dict[int, list[str]] = {}
    for oid, _, coll in links:
        if coll:
            collections.setdefault(oid, []).append(coll)
    for r in c.execute("SELECT id, creator, tags FROM mmf_items"):
        if r["id"] in new_ids:
            continue
        tags = json.loads(r["tags"] or "[]") + own.get(r["id"], [])
        by_creator.setdefault(creator_key(display_creator(r["creator"], names)), []).append(tags)
        for coll in collections.get(r["id"], []):
            by_collection.setdefault(coll.casefold(), []).append(tags)
    local: dict[str, tuple[str, list[str]]] = {}
    for r in c.execute("SELECT f.model_id, MAX(f.creator) creator, t.tag FROM model_tags t "
                       "JOIN files f ON f.model_id = t.model_id AND f.hidden = 0 GROUP BY f.model_id, t.tag"):
        local.setdefault(r["model_id"], (r["creator"], []))[1].append(r["tag"])
    for creator, tags in local.values():
        if creator:
            by_creator.setdefault(creator_key(display_creator(creator, names)), []).append(tags)
    # Spelled the way the library already spells them ("Fantasy" if that's what's there).
    spelling = {r[0].casefold(): r[0] for r in c.execute(
        "SELECT DISTINCT tag FROM mmf_item_tags UNION ALL SELECT DISTINCT tag FROM model_tags")}
    tagged = 0
    for r in new:
        site = json.loads(r["tags"] or "[]")
        mine = creator_key(display_creator(r["creator"], names)) if r["creator"] else ""
        siblings = [t for coll in collections.get(r["id"], []) for t in by_collection.get(coll.casefold(), [])]
        genre = (autotag.genre_hint(siblings) if len(siblings) >= 2 else "") or \
            autotag.genre_hint(by_creator.get(mine, []) if mine else [])
        race = autotag.race_hint(by_creator.get(mine, [])) if mine else ""
        have = {t.casefold() for t in site + own.get(r["id"], [])}
        add = [spelling.get(t, t) for t in autotag.guess(r["name"], site, " ".join(collections.get(r["id"], [])), genre, race)
               if t.casefold() not in have]
        c.executemany("INSERT OR IGNORE INTO mmf_item_tags(item_id, tag) VALUES (?,?)", [(r["id"], t) for t in add])
        tagged += bool(add)
    c.executemany("INSERT OR IGNORE INTO mmf_autotagged(item_id) VALUES (?)", [(i,) for i in new_ids])
    return tagged


def clear():
    c = db.conn()
    with c:
        c.execute("DELETE FROM mmf_links")
        c.execute("DELETE FROM mmf_items")
        c.execute("DELETE FROM settings WHERE key='mmf_synced'")


# ---------------------------------------------------------------- matching to the local library

# Words in shop titles that don't name the thing itself.
_GENERIC = {
    "bundle", "release", "releases", "collection", "pack", "set", "kit", "miniature", "miniatures",
    "mini", "minis", "figure", "figures", "tabletop", "wargaming", "wargame", "dnd", "rpg", "of", "a", "an",
    "with", "by", "in", "x", "edition", "complete", "full", "patreon", "tribe", "kickstarter", "ks",
    "for", "pre", "supported", "presupported", "unsupported", "stl", "stls", "base", "bases",
}
_MONTHS = {m: str(i) for i, ms in enumerate((
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
    ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
    ("november", "nov"), ("december", "dec")), 1) for m in ms}


def _tokens(name: str) -> set[str]:
    """Comparable words of a name: 'Dwarf_Warriors - Sept 2023 (Supported)' -> {dwarf, warriors, 9, 2023}."""
    out = set()
    for w in classify.name_key(name).split():
        if re.fullmatch(r"(?:19|20)\d\d(?:0[1-9]|1[0-2])", w):  # 202309
            out.update((w[:4], str(int(w[4:]))))
            continue
        w = _MONTHS.get(w, w)
        if w.isdigit():
            w = str(int(w))
        if re.fullmatch(r"[0-9a-f]{8,}", w) and re.search(r"\d", w):
            continue  # an upload id ("602ab09167439 angel-fighter"), not part of the name
        if w not in _GENERIC and not re.fullmatch(r"\d+mm", w):  # 32mm: a scale, not a name
            out.add(w)
    return out


# Words that only say what kind of shop it is: "Bestiarum Miniatures" is "Bestiarum".
_SHOP_WORDS = {"miniatures", "miniature", "minis", "studio", "studios", "official", "the"}


def creator_key(name: str) -> str:
    """Comparison key for creator names, so 'CobraMode', 'Cobra Mode' and 'cobra_mode!' are one creator:
    accents and special characters ignored, CamelCase split into words, shop words left out."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    words = re.findall(r"[a-z0-9]+", s.lower())
    return "".join(w for w in words if w not in _SHOP_WORDS) or "".join(words)


def creator_names() -> dict[str, str]:
    """creator_key -> the name the user gave that creator in the app."""
    return {r["key"]: r["name"] for r in db.conn().execute("SELECT key, name FROM creator_names")}


def display_creator(name: str, names: dict[str, str]) -> str:
    """How a creator name found in the files or on MyMiniFactory is shown."""
    return names.get(creator_key(name), name) if name else name


def rename_creator(old: str, new: str) -> str:
    """Show creator `old` (as currently shown) as `new` from now on; blank `new` undoes renames.
    Covers every spelling already shown as `old`, so renaming twice keeps working."""
    new = " ".join(new.split())[:200]
    c = db.conn()
    keys = {creator_key(old)} | {k for k, v in creator_names().items() if v.casefold() == old.casefold()}
    keys.discard("")
    for k in keys:
        if new:
            c.execute("INSERT INTO creator_names(key, name) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET name=excluded.name", (k, new))
        else:
            c.execute("DELETE FROM creator_names WHERE key=?", (k,))
    c.commit()
    return new


class _Index:
    """Local releases and models by word, to match MyMiniFactory titles against."""

    def __init__(self):
        self.entries = []  # (tokens, creator tokens, creator key, release, model_id or None)
        self.by_word: dict[str, list[int]] = {}
        seen_releases = set()
        # Creators renamed in the app count as one ("Lord of the Print" shown as "Rescale Miniatures").
        self.names = creator_names()
        for r in db.conn().execute("SELECT release, model, MAX(creator) creator, MIN(model_id) model_id "
                                   "FROM files WHERE hidden=0 GROUP BY release, model"):
            creator = _tokens(r["creator"] or "")
            ckey = creator_key(display_creator(r["creator"] or "", self.names))
            if r["release"] not in seen_releases:
                seen_releases.add(r["release"])
                self._add(_tokens(r["release"]) - creator or _tokens(r["release"]), creator, ckey, r["release"], None)
            self._add(_tokens(r["model"]) - creator or _tokens(r["model"]), creator, ckey, r["release"], r["model_id"])

    def _add(self, toks, creator, ckey, release, model_id):
        if not toks:
            return
        i = len(self.entries)
        self.entries.append((toks, creator, ckey, release, model_id))
        for w in toks:
            self.by_word.setdefault(w, []).append(i)

    def match(self, name: str, creator: str = ""):
        ctoks, ckey = _tokens(creator), creator_key(display_creator(creator, self.names))
        toks = _tokens(name)
        toks = toks - ctoks or toks
        if not toks:
            return None
        best, best_score = None, 0.0
        for i in {i for w in toks for i in self.by_word.get(w, ())}:
            etoks, ecreator, ekey, release, model_id = self.entries[i]
            common = len(toks & etoks)
            jaccard = common / len(toks | etoks)
            same_creator = bool(ckey) and (ckey == ekey or (bool(ctoks) and bool(ecreator)
                                                         and (ctoks <= ecreator or ecreator <= ctoks)))
            contained = common == min(len(toks), len(etoks))
            if not same_creator and all(w.isdigit() for w in toks & etoks):
                continue  # "September 2023" alone says nothing without the creator
            # By the same creator a shorter name inside a longer one is enough with two shared words,
            # or one long word in a name of at most two ("Witch" alone isn't "Zelina the Witch Empress").
            # Otherwise the names must share two words and be nearly the same.
            if same_creator:
                ok = jaccard >= 0.6 or (contained and (
                    common >= 2 or (jaccard >= 0.5 and any(len(w) >= 5 for w in toks & etoks))))
            else:  # another or an unknown creator: "Welcome Pack" isn't everyone's welcome pack
                ok = common >= 2 and jaccard >= 0.75
            if not ok:
                continue
            score = jaccard + (0.3 if same_creator else 0) + (0.05 if model_id is None else 0)
            if score > best_score:
                best, best_score = {"release": release, "model_id": model_id}, score
        return best


_index_cache: tuple = (None, None)


def _local_index() -> _Index:
    """Built once per change of the indexed files (and corrections), not per request."""
    global _index_cache
    sig = tuple(db.conn().execute(
        "SELECT COUNT(*), MAX(id), TOTAL(length(release) + length(model) + length(creator) + hidden) FROM files"
    ).fetchone()) + (tuple(sorted(creator_names().items())),)
    if _index_cache[0] != sig:
        _index_cache = (sig, _Index())
    return _index_cache[1]


def _match(name: str, creator: str, index: _Index):
    return index.match(name, creator)


def _search(q: str) -> tuple[str, list]:
    where, args = [], []
    for word in q.split():
        if word.lower().startswith("tag:") and len(word) > 4:  # exact tag search
            where.append("(EXISTS (SELECT 1 FROM json_each(i.tags) WHERE value = ? COLLATE NOCASE) "
                         "OR i.id IN (SELECT item_id FROM mmf_item_tags WHERE tag = ? COLLATE NOCASE))")
            args += [word[4:]] * 2
            continue
        where.append("(i.name LIKE ? OR i.creator LIKE ? OR i.id IN "
                     "(SELECT item_id FROM mmf_links WHERE collection LIKE ?) "
                     "OR EXISTS (SELECT 1 FROM json_each(i.tags) WHERE value LIKE ?) "
                     "OR i.id IN (SELECT item_id FROM mmf_item_tags WHERE tag LIKE ?))")
        args += [f"%{word}%"] * 5
    return ("WHERE " + " AND ".join(where) if where else ""), args


def _release_of(d: dict) -> str:
    """The release an item belongs to on MyMiniFactory: its tribe month or pledge, else the item itself."""
    return next((s["collection"] for s in d["sources"] if s["collection"]), "") or d["name"]


def _matched_items(q: str = "", creator: str | None = None) -> list[dict]:
    """Items with their sources, their matching local release or model (local) and their release."""
    c = db.conn()
    where, args = _search(q)
    rows = c.execute(f"""SELECT i.* FROM mmf_items i {where}
                         ORDER BY i.creator COLLATE NOCASE, i.name COLLATE NOCASE""", args).fetchall()
    names = creator_names()
    own = own_tags()
    if creator is not None:
        key = creator_key(creator)
        rows = [r for r in rows if creator_key(display_creator(r["creator"], names)) == key]
    link_map: dict[int, list] = {}
    for r in c.execute("SELECT * FROM mmf_links ORDER BY source, collection"):
        link_map.setdefault(r["item_id"], []).append({"source": r["source"], "collection": r["collection"]})
    index = _local_index()
    out = []
    for r in rows:
        d = _item(r, names, own)
        d["sources"] = link_map.get(r["id"], [])
        d["local"] = _match(r["name"], d["creator"], index)
        d["release"] = _release_of(d)
        out.append(d)
    return out


def items(q: str = "", missing: bool = False, offset: int = 0, limit: int = 200,
          creator: str | None = None, release: str | None = None) -> dict:
    """creator: only items by this creator (compared with creator_key; '' = items without one).
    release: only items of this MyMiniFactory release that aren't in the local library."""
    out = _matched_items(q, creator)
    if missing or release is not None:
        out = [d for d in out if not d["local"]]
    if release is not None:
        out = [d for d in out if d["release"].casefold() == release.casefold()]
    return {"total": len(out), "items": out[offset:offset + limit]}


def grid_items(q: str = "", creator: str | None = None, release: str | None = None, source: str = "",
               tags: list[str] = ()) -> list[dict]:
    """Items for the main grid, shown like local models. source '' or 'missing': items not in the
    library; 'mmf': all of them, the ones in the library under their local release (grid_release)."""
    out = []
    for d in _matched_items(q, creator):
        if d["local"] and source != "mmf":
            continue
        d["grid_release"] = d["local"]["release"] if d["local"] else d["release"]
        if release is not None and d["grid_release"].casefold() != release.casefold():
            continue
        if tags and not has_tags(d["tags"] + d["mmf_tags"], tags):
            continue
        d["mmf"] = True
        out.append(d)
    return out


def releases(q: str = "", creator: str | None = None, source: str = "", tags: list[str] = ()) -> dict[str, dict]:
    """casefolded release -> {"release", "creator", "mmf": count} of the grid items (see grid_items)."""
    groups: dict[str, dict] = {}
    for d in grid_items(q, creator, None, source, tags):
        g = groups.setdefault(d["grid_release"].casefold(), {"release": d["grid_release"], "creators": {}, "mmf": 0})
        g["mmf"] += 1
        if d["creator"]:
            g["creators"][d["creator"]] = g["creators"].get(d["creator"], 0) + 1
    return {k: {"release": g["release"], "creator": max(g["creators"], key=g["creators"].get) if g["creators"] else None,
                "mmf": g["mmf"]} for k, g in groups.items()}


def own_tags(ids: list[int] | None = None) -> dict[int, list[str]]:
    """item id -> the tags added to it in the app."""
    out: dict[int, list[str]] = {}
    sql = "SELECT item_id, tag FROM mmf_item_tags"
    if ids is not None:
        sql += f" WHERE item_id IN ({','.join('?' * len(ids))})"
    for r in db.conn().execute(sql + " ORDER BY tag COLLATE NOCASE", ids or []):
        out.setdefault(r["item_id"], []).append(r["tag"])
    return out


def set_tags(ids: list[int], add: list[str], remove: list[str]) -> int:
    """Add and remove the app's own tags on items; returns how many items exist. A tag the item
    already has from MyMiniFactory isn't added again."""
    c = db.conn()
    site = {r["id"]: {t.casefold() for t in json.loads(r["tags"] or "[]")} for r in c.execute(
        f"SELECT id, tags FROM mmf_items WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    with c:
        for oid in site:
            for t in remove:
                c.execute("DELETE FROM mmf_item_tags WHERE item_id=? AND tag=? COLLATE NOCASE", (oid, t))
            for t in add:
                if t.casefold() not in site[oid] and not c.execute(
                        "SELECT 1 FROM mmf_item_tags WHERE item_id=? AND tag=? COLLATE NOCASE", (oid, t)).fetchone():
                    c.execute("INSERT INTO mmf_item_tags(item_id, tag) VALUES (?,?)", (oid, t))
    return len(site)


def _item(r, names: dict[str, str] | None = None, own: dict[int, list[str]] | None = None) -> dict:
    d = dict(r)
    d["creator"] = display_creator(d["creator"], creator_names() if names is None else names)
    gallery = json.loads(d.pop("gallery", "") or "[]")
    d["images"] = gallery or json.loads(d.get("images") or "[]") or ([d["image"]] if d.get("image") else [])
    d["downloads"] = len(json.loads(d.get("downloads") or "[]"))
    d["queued"] = bool(d.get("queued"))
    d["mmf_tags"] = json.loads(d.pop("tags", "") or "[]")  # from MyMiniFactory
    d["tags"] = (own_tags([d["id"]]) if own is None else own).get(d["id"], [])  # added in the app
    return d


_local_tags_cache: tuple = (None, {})


def local_tags() -> dict[str, list[str]]:
    """model_id -> tags of the MyMiniFactory items matched to that local model. Not stored on the
    model: they follow the match, and stay out of the model's own (editable) tags."""
    global _local_tags_cache
    _local_index()
    sig = (_index_cache[0],) + tuple(db.conn().execute(
        "SELECT COUNT(*), MAX(updated), TOTAL(length(tags)) FROM mmf_items").fetchone())
    if _local_tags_cache[0] != sig:
        out: dict[str, list[str]] = {}
        for d in _matched_items():
            mid = (d["local"] or {}).get("model_id")
            if mid and d["mmf_tags"]:
                have = out.setdefault(mid, [])
                have += [t for t in d["mmf_tags"] if t.casefold() not in {x.casefold() for x in have}]
        _local_tags_cache = (sig, out)
    return _local_tags_cache[1]


def creators(q: str = "", tags: list[str] = ()) -> dict[str, dict]:
    """creator_key -> {"creator": the most used spelling, "items": count} of the synced items."""
    where, args = _search(q)
    spellings: dict[str, dict[str, int]] = {}
    renamed = creator_names()
    own = own_tags() if tags else {}
    for r in db.conn().execute(f"SELECT i.id, i.creator, i.tags FROM mmf_items i {where}", args):
        if tags and not has_tags(json.loads(r["tags"] or "[]") + own.get(r["id"], []), tags):
            continue
        shown = display_creator(r["creator"], renamed)
        names = spellings.setdefault(creator_key(shown), {})
        names[shown] = names.get(shown, 0) + 1
    return {k: {"creator": max(v, key=v.get), "items": sum(v.values())} for k, v in spellings.items()}


def item(oid: int) -> dict | None:
    c = db.conn()
    r = c.execute("SELECT * FROM mmf_items WHERE id=?", (oid,)).fetchone()
    if not r:
        return None
    d = _item(r)
    d["sources"] = [dict(x) for x in c.execute(
        "SELECT source, collection FROM mmf_links WHERE item_id=? ORDER BY source, collection", (oid,))]
    d["local"] = _match(r["name"], d["creator"], _local_index())
    return d


def set_queued(oid: int, queued: bool) -> bool:
    c = db.conn()
    with c:
        n = c.execute("UPDATE mmf_items SET queued=?, download_note='' WHERE id=?", (int(queued), oid)).rowcount
    return bool(n)


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _folder_name(s: str) -> str:
    return _UNSAFE.sub("-", s).strip(" .-")[:120] or "MyMiniFactory"


def queue() -> list[dict]:
    """Items to download on the next bookmarklet run, with the library folder each goes to:
    Creator/Item (read as Creator / Release) or just Item when the creator is unknown."""
    out, names = [], creator_names()
    for r in db.conn().execute("SELECT * FROM mmf_items WHERE queued=1 ORDER BY name COLLATE NOCASE"):
        name = _folder_name(r["name"])
        creator = _folder_name(display_creator(r["creator"], names)) if r["creator"] else ""
        out.append({"id": r["id"], "name": r["name"], "url": r["url"],
                     "downloads": json.loads(r["downloads"] or "[]"),
                     "folder": f"{creator}/{name}" if creator else name, "depth": 1 if creator else 0})
    return out


def gallery_needed() -> list[dict]:
    """Items whose page hasn't been read for its full-size images yet."""
    return [{"id": r["id"], "url": r["url"], "image": r["image"]} for r in db.conn().execute(
        "SELECT id, url, image FROM mmf_items WHERE gallery='' ORDER BY id")]


def set_galleries(entries: list) -> int:
    """Store the images the bookmarklet read from item pages: [{"id", "images": [url, ...]}]."""
    rows = []
    for e in entries[:MAX_ITEMS]:
        if not isinstance(e, dict) or not isinstance(e.get("images"), list):
            continue
        try:
            oid = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        images = list(dict.fromkeys(u for u in (_url(x) for x in e["images"][:60] if isinstance(x, str)) if u))
        rows.append((json.dumps(images), oid))
    c = db.conn()
    with c:
        c.executemany("UPDATE mmf_items SET gallery=? WHERE id=?", rows)
    return len(rows)


def download_result(oid: int, ok: bool, note: str):
    c = db.conn()
    with c:
        c.execute("UPDATE mmf_items SET queued=?, download_note=? WHERE id=?", (0 if ok else 1, _text(note, 500), oid))


def status() -> dict:
    c = db.conn()
    total = c.execute("SELECT COUNT(*) FROM mmf_items").fetchone()[0]
    synced = c.execute("SELECT value FROM settings WHERE key='mmf_synced'").fetchone()
    by_source = {r["source"]: r["n"] for r in c.execute(
        "SELECT source, COUNT(DISTINCT item_id) n FROM mmf_links GROUP BY source")}
    missing = 0
    if total:
        index = _local_index()
        missing = sum(1 for r in c.execute("SELECT name, creator FROM mmf_items") if not _match(r["name"], r["creator"], index))
    queued = c.execute("SELECT COUNT(*) FROM mmf_items WHERE queued=1").fetchone()[0]
    return {"total": total, "missing": missing, "sources": by_source, "queued": queued,
            "synced": float(synced[0]) if synced else None}
