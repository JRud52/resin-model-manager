"""Read .zip and .7z archives without extracting them to the library."""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

import py7zr

from . import config


@dataclass
class Member:
    name: str
    size: int
    mtime: float


def list_members(path: Path) -> list[Member]:
    ext = path.suffix.lower()
    out: list[Member] = []
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = _fix_zip_name(info)
                try:
                    mtime = datetime(*info.date_time).timestamp()
                except ValueError:
                    mtime = 0.0
                out.append(Member(name, info.file_size, mtime))
    elif ext == ".7z":
        with py7zr.SevenZipFile(path, "r") as z:
            for info in z.list():
                if info.is_directory:
                    continue
                mtime = info.creationtime.timestamp() if info.creationtime else 0.0
                out.append(Member(info.filename, info.uncompressed or 0, mtime))
    return out


def _fix_zip_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    # Zips made on Windows without the UTF-8 flag are cp437 per spec but often actually cp1252/utf-8.
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name.replace("\\", "/")


def _zip_lookup(z: zipfile.ZipFile, member: str) -> zipfile.ZipInfo:
    for info in z.infolist():
        if _fix_zip_name(info) == member:
            return info
    raise KeyError(member)


def read_member(path: Path, member: str) -> bytes:
    ext = path.suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            return z.read(_zip_lookup(z, member))
    if ext == ".7z":
        for name, data in iter_members(path, [member]):
            return data
        raise KeyError(member)
    raise ValueError(f"not an archive: {path}")


def open_member_stream(path: Path, member: str):
    """File-like object for streaming a download."""
    ext = path.suffix.lower()
    if ext == ".zip":
        z = zipfile.ZipFile(path)
        return z.open(_zip_lookup(z, member))
    import io
    return io.BytesIO(read_member(path, member))


def iter_members(path: Path, members: list[str]) -> Iterator[tuple[str, bytes]]:
    """Yield (member, bytes) for several members, opening the archive once.

    7z archives are usually solid, so pulling members one at a time would
    decompress the archive over and over. They are extracted together into a
    temporary folder under DATA_DIR and read back one by one.
    """
    ext = path.suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            lookup = {_fix_zip_name(i): i for i in z.infolist()}
            for m in members:
                if m in lookup:
                    yield m, z.read(lookup[m])
        return
    if ext == ".7z":
        config.TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=config.TMP_DIR))
        try:
            with py7zr.SevenZipFile(path, "r") as z:
                z.extract(path=tmp, targets=list(members))
            for m in members:
                f = tmp / m
                if f.is_file():
                    yield m, f.read_bytes()
                    f.unlink()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return
    raise ValueError(f"not an archive: {path}")
