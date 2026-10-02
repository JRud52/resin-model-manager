"""Heuristics that turn a messy creator folder layout into release / model / part.

Every file gets a *logical path*: its path inside the library, with archives
treated as folders (``Release/Heroes.zip`` -> ``Release/Heroes/...``).
From that path we guess:

* release   - the top folder (after RELEASE_DEPTH creator levels)
* model     - the first folder below the release that is not a "noise" folder
              like ``Supported``, ``STL``, ``32mm`` or ``Lychee``; or, when
              files sit loose, the shared name prefix of sibling files
* option    - any meaningful folders between the model and the file
              (e.g. ``Heads``, ``Weapon options``)
* supported - True / False / None from folder or file names

Users correct mistakes with override rules keyed on a logical path prefix.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_UNSUP = re.compile(r"\b(?:un|non|no|without)\s?(?:pre\s?)?sup(?:p?orted|ports?|p)?\b|\bunsup\w*|\bnosup\w*")
_SUP = re.compile(r"\b(?:pre\s?)?sup(?:p?orted|ports?)\b|\bpresup\w*|\bpre\s?supp?\w*|\bsup\b|\bsupp\b|\bwith\s+supports?\b")
_SCALE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:mm|cm|%)\b|\bscale\b|\bheroic\b")
_NOISE_WORDS = {
    "stl", "stls", "lys", "lychee", "ctx", "ctb", "chitubox", "chitu", "obj", "3mf",
    "file", "files", "model", "models", "print", "printing", "for", "and", "the", "3d", "ready",
    "version", "versions", "ver", "v", "parts", "folder", "resin", "fdm", "hollowed", "hollow",
    "solid", "split", "unsplit", "keyed", "keys", "presupported", "supported", "unsupported",
}
_PART_SUFFIX = re.compile(r"(?:\s(?:part|pt|p))?\s?\d{1,3}[a-z]?$")


def norm(s: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)  # CamelCase -> words
    s = re.sub(r"[_\-.+,()\[\]{}]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def support_flag(text: str) -> bool | None:
    t = norm(text)
    if _UNSUP.search(t):
        return False
    if _SUP.search(t):
        return True
    return None


def strip_tokens(name: str, release: str = "") -> str:
    """Remove support/scale/format words, return a clean display name ('' if nothing left)."""
    t = norm(name)
    if release:
        r = norm(release)
        if r:
            t = t.replace(r, " ")
    t = _UNSUP.sub(" ", t)
    t = _SUP.sub(" ", t)
    t = _SCALE.sub(" ", t)
    words = [w for w in t.split() if w not in _NOISE_WORDS]
    return " ".join(words).strip()


def is_noise(folder: str, release: str) -> bool:
    left = strip_tokens(folder, release)
    return not left or left.isdigit()


def pretty(name: str) -> str:
    """Readable display version of a raw folder or file name."""
    s = re.sub(r"[_]+", " ", name).strip()
    return re.sub(r"\s+", " ", s) or name


def pretty_clean(name: str, release: str = "") -> str:
    """Display name with support markers removed, keeping original capitalisation."""
    s = pretty(name)
    s = re.sub(r"(?i)[\s\-_(\[]*\b(?:un|non|no)?[\s\-_]?(?:pre)?[\s\-_]?supp?(?:orted|orts?)?\b[\s)\]]*", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return s or pretty(name)


@dataclass
class Guess:
    creator: str
    release: str
    model: str
    option: str
    supported: bool | None
    model_root: str  # logical path prefix identifying this model (used for overrides)
    # What each folder of the path was read as, up to the model folder (see ROLES).
    roles: tuple[str, ...] = ()
    release_index: int = -1  # position of the release folder in the path, -1 if none


def name_key(name: str) -> str:
    """Comparison key for folder and creator names ('Bite_The_Bullet' == 'Bite The Bullet')."""
    return strip_tokens(name) or norm(name)


def parse_keys(text: str) -> set[str]:
    """Comma-separated folder names (the Ignored folders setting) -> name keys."""
    return {name_key(n) for n in text.split(",") if n.strip()} - {""}


def layout(folders: list[str], release_depth: int, ignored_keys: set[str] | None = None,
           creator_keys: set[str] | None = None) -> tuple[int, int]:
    """(leading folders to ignore, folders above the release after them).

    Leading folders such as ``Freebies`` are not creators. Below them, a folder named
    after a known creator is the creator (Freebies/Creator/Release/...); anything else
    is the release (Freebies/Release/...)."""
    skip = 0
    while skip < len(folders) and name_key(folders[skip]) in (ignored_keys or ()):
        skip += 1
    if skip == len(folders) and skip:
        skip -= 1  # files loose in Freebies/: the folder itself is their release
    if not skip:
        return 0, release_depth
    rest = folders[skip:]
    return skip, 1 if len(rest) > 1 and name_key(rest[0]) in (creator_keys or ()) else 0


def guess(logical_path: str, release_depth: int = 0, sibling_stems: list[str] | None = None,
          creator_keys: set[str] | None = None, ignored_keys: set[str] | None = None) -> Guess:
    """creator_keys: name_key() of known creators. A folder below the release that just
    repeats a creator name (Creator/Release/Creator/Model) is skipped like a noise folder.
    ignored_keys: leading folders that are neither creator nor release (see layout())."""
    p = PurePosixPath(logical_path)
    folders = list(p.parts[:-1])
    skipped, release_depth = layout(folders, release_depth, ignored_keys, creator_keys)
    rel_i = skipped + release_depth
    if len(folders) <= rel_i:  # not deep enough to have a release folder
        return _tail(logical_path, folders, " / ".join(folders[skipped:]), -1, len(folders),
                     sibling_stems, creator_keys, ("ignore",) * skipped + ("creator",) * (len(folders) - skipped))
    roles = ("ignore",) * skipped + ("creator",) * release_depth + ("release",)
    return _tail(logical_path, folders, " / ".join(folders[skipped:rel_i]), rel_i, rel_i + 1,
                 sibling_stems, creator_keys, roles)


# Roles a user can give the folders of a path in a folder mapping. Folders below the
# model (or below the release when no model is chosen) are read automatically.
ROLES = ("creator", "release", "model", "ignore")


def check_roles(roles: list[str]) -> tuple[str, ...]:
    """Validate a folder mapping and trim it to the last folder that has a role."""
    roles = [r if r in ROLES else "auto" for r in roles]
    if roles.count("release") != 1:
        raise ValueError("Pick exactly one Release folder")
    rel_i = roles.index("release")
    if any(r == "creator" for r in roles[rel_i:]):
        raise ValueError("Creator folders must come before the Release folder")
    if roles.count("model") > 1:
        raise ValueError("Pick at most one Model folder")
    if "model" in roles and roles.index("model") < rel_i:
        raise ValueError("The Model folder must come after the Release folder")
    end = roles.index("model") if "model" in roles else rel_i
    if any(r != "auto" for r in roles[end + 1:]):
        raise ValueError("Folders below the model are read automatically; leave them on Automatic")
    # Above the model, a folder without a role is skipped.
    return tuple("ignore" if r == "auto" else r for r in roles[:end + 1])


def mapped(logical_path: str, roles: tuple[str, ...], sibling_stems: list[str] | None = None,
           creator_keys: set[str] | None = None) -> Guess:
    """Read a path with a folder mapping the user set (see check_roles)."""
    folders = list(PurePosixPath(logical_path).parts[:-1])
    rel_i = roles.index("release")
    creator = " / ".join(f for f, r in zip(folders, roles) if r == "creator")
    if len(folders) <= rel_i:
        return _tail(logical_path, folders, creator, -1, len(folders), sibling_stems, creator_keys,
                     roles[:len(folders)])
    model_i = roles.index("model") if "model" in roles else -1
    return _tail(logical_path, folders, creator, rel_i, model_i if model_i >= 0 else rel_i + 1,
                 sibling_stems, creator_keys, roles, forced_model=model_i >= 0)


def _tail(logical_path: str, folders: list[str], creator: str, rel_i: int, rest_i: int,
          sibling_stems, creator_keys, roles, forced_model: bool = False) -> Guess:
    """Shared end of guess() and mapped(): model, options and support from the folders
    from rest_i down (the first one is the model when forced_model)."""
    stem = PurePosixPath(logical_path).stem
    release_raw = folders[rel_i] if rel_i >= 0 else ""
    release = pretty_clean(release_raw) if release_raw else "Unsorted"
    rest = folders[rest_i:]

    supported = None
    for part in [*folders[rel_i + 1:], stem] if rel_i >= 0 else [stem]:
        flag = support_flag(part)
        if flag is not None:
            supported = flag  # deepest marker wins
    if supported is None and release_raw:
        supported = support_flag(release_raw)

    skip = {name_key(f) for f, r in zip(folders, roles) if r == "creator"} | (creator_keys or set())
    meaningful = [(i, f) for i, f in enumerate(rest)
                  if (forced_model and i == 0) or (not is_noise(f, release_raw) and name_key(f) not in skip)]
    if meaningful:
        idx, mfolder = meaningful[0]
        model = pretty_clean(mfolder)
        model_key = strip_tokens(mfolder)
        options = [pretty_clean(f) for _, f in meaningful[1:] if strip_tokens(f) != model_key]
        model_i = rest_i + idx
        roles = tuple(roles[:model_i]) + ("ignore",) * (model_i - len(roles)) + ("model",)
        return Guess(creator, release, model, " / ".join(options), supported,
                     "/".join(folders[:model_i + 1]), roles, rel_i)

    # Loose files: derive the model from the file name.
    model = _model_from_stem(stem, sibling_stems or [])
    return Guess(creator, release, model, "", supported, logical_path, tuple(roles[:len(folders)]), rel_i)


def _words(stem: str) -> list[str]:
    t = strip_tokens(stem)
    t = _PART_SUFFIX.sub("", t).strip()
    return t.split() or norm(stem).split()


def _model_from_stem(stem: str, siblings: list[str]) -> str:
    """Longest word prefix shared with a sibling file, else the cleaned stem."""
    mine = _words(stem)
    best: list[str] = []
    for other in siblings:
        if other == stem:
            continue
        ow = _words(other)
        n = 0
        while n < min(len(mine), len(ow)) and mine[n] == ow[n]:
            n += 1
        if n > len(best):
            best = mine[:n]
    words = best or mine
    return " ".join(w.capitalize() if w.islower() else w for w in words) or pretty(stem)
