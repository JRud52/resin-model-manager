"""STL parsing and a pure-numpy software renderer.

No GPU or OpenGL is needed, so it runs in a slim container on any NAS CPU.
Small triangles are splatted as points and larger ones are rasterized in
size buckets, which keeps multi-million triangle resin models fast.
"""
from __future__ import annotations

import io
import re
import struct

import numpy as np
from PIL import Image, ImageOps

MAX_TRIANGLES = 3_000_000  # above this, triangles are subsampled for the preview

_BIN_DTYPE = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])
_FLOAT = rb"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_VERTEX_RE = re.compile(rb"vertex\s+(" + _FLOAT + rb")\s+(" + _FLOAT + rb")\s+(" + _FLOAT + rb")")


def parse_stl(data: bytes) -> np.ndarray:
    """Return an (n, 3, 3) float32 array of triangle vertices."""
    if len(data) >= 84:
        (count,) = struct.unpack_from("<I", data, 80)
        if 84 + count * 50 == len(data) or not data[:5].lower() == b"solid":
            count = min(count, (len(data) - 84) // 50)
            tris = np.frombuffer(data, dtype=_BIN_DTYPE, count=count, offset=84)
            return np.ascontiguousarray(tris["v"])
    verts = np.array(_VERTEX_RE.findall(data), dtype=np.float32)
    n = len(verts) // 3
    return verts[: n * 3].reshape(n, 3, 3)


def _rotation(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    az, el = np.radians(azimuth_deg), np.radians(elevation_deg)
    # Z-up model space. Spin around Z, then tilt the camera down.
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    # Map to camera space: x right, y up, z towards viewer.
    to_cam = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=float)
    rx = np.array([[1, 0, 0], [0, np.cos(el), -np.sin(el)], [0, np.sin(el), np.cos(el)]])
    return (rx @ to_cam @ rz).astype(np.float32)


def render_triangles(tris: np.ndarray, size: int = 512, supersample: int = 2,
                     azimuth: float = -35.0, elevation: float = 22.0,
                     color=(200, 212, 226)) -> Image.Image:
    tris = tris[np.isfinite(tris).all(axis=(1, 2))]
    if len(tris) == 0:
        raise ValueError("no triangles")
    if len(tris) > MAX_TRIANGLES:
        idx = np.random.default_rng(0).choice(len(tris), MAX_TRIANGLES, replace=False)
        tris = tris[idx]

    W = size * supersample
    pad = 0.06
    v = tris.reshape(-1, 3)
    center = (v.min(0) + v.max(0)) / 2
    rot = _rotation(azimuth, elevation)
    p = ((tris - center) @ rot.T).astype(np.float32)  # camera space
    lo, hi = p.reshape(-1, 3).min(0), p.reshape(-1, 3).max(0)
    extent = float(max(hi[0] - lo[0], hi[1] - lo[1])) or 1.0
    scale = W * (1 - 2 * pad) / extent
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    sx = (p[..., 0] - cx) * scale + W / 2
    sy = W / 2 - (p[..., 1] - cy) * scale
    sz = -p[..., 2]  # smaller = closer

    # Shading from geometric normals (two-sided, so bad winding still looks fine).
    n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    key = np.array([-0.35, 0.55, 0.76], dtype=np.float32)
    key /= np.linalg.norm(key)
    fill = np.array([0.6, -0.2, 0.5], dtype=np.float32)
    fill /= np.linalg.norm(fill)
    nf = np.where(n[:, 2:3] < 0, -n, n)
    shade = 0.18 + 0.62 * np.clip(nf @ key, 0, 1) + 0.25 * np.clip(nf @ fill, 0, 1) ** 2
    shade = np.clip(shade, 0, 1.1).astype(np.float32)

    zbuf = np.full(W * W, np.inf, dtype=np.float32)
    sbuf = np.zeros(W * W, dtype=np.float32)

    def commit(pix, depth, sh):
        ok = (pix >= 0)
        pix, depth, sh = pix[ok], depth[ok], sh[ok]
        if len(pix) == 0:
            return
        order = np.lexsort((depth, pix))
        pix, depth, sh = pix[order], depth[order], sh[order]
        first = np.ones(len(pix), dtype=bool)
        first[1:] = pix[1:] != pix[:-1]
        pix, depth, sh = pix[first], depth[first], sh[first]
        closer = depth < zbuf[pix]
        zbuf[pix[closer]] = depth[closer]
        sbuf[pix[closer]] = sh[closer]

    x0, x1 = np.floor(sx.min(1)), np.ceil(sx.max(1))
    y0, y1 = np.floor(sy.min(1)), np.ceil(sy.max(1))
    span = np.maximum(x1 - x0, y1 - y0)

    # Tiny triangles: splat vertices and centroid.
    tiny = span <= 1
    if tiny.any():
        px = np.concatenate([sx[tiny], sx[tiny].mean(1, keepdims=True)], axis=1).ravel()
        py = np.concatenate([sy[tiny], sy[tiny].mean(1, keepdims=True)], axis=1).ravel()
        pz = np.concatenate([sz[tiny], sz[tiny].mean(1, keepdims=True)], axis=1).ravel()
        ps = np.repeat(shade[tiny], 4)
        ix, iy = px.astype(np.int64), py.astype(np.int64)
        inb = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < W)
        commit(np.where(inb, iy * W + ix, -1), pz, ps)

    # Larger triangles: rasterize in power-of-two bucket sizes.
    rest = np.nonzero(~tiny)[0]
    bucket = np.ceil(np.log2(np.maximum(span[rest], 2))).astype(int)
    for b in np.unique(bucket):
        sel = rest[bucket == b]
        s = int(2 ** b) + 1
        gy, gx = np.mgrid[0:s, 0:s]
        gx, gy = gx.ravel().astype(np.float32), gy.ravel().astype(np.float32)
        chunk = max(1, 4_000_000 // (s * s))
        for i in range(0, len(sel), chunk):
            t = sel[i:i + chunk]
            ax, ay, bx, by, cx_, cy_ = sx[t, 0], sy[t, 0], sx[t, 1], sy[t, 1], sx[t, 2], sy[t, 2]
            X = x0[t, None] + gx[None] + 0.5
            Y = y0[t, None] + gy[None] + 0.5
            den = (by - cy_) * (ax - cx_) + (cx_ - bx) * (ay - cy_)
            den = np.where(np.abs(den) < 1e-12, 1e-12, den)[:, None]
            w0 = ((by - cy_)[:, None] * (X - cx_[:, None]) + (cx_ - bx)[:, None] * (Y - cy_[:, None])) / den
            w1 = ((cy_ - ay)[:, None] * (X - cx_[:, None]) + (ax - cx_)[:, None] * (Y - cy_[:, None])) / den
            w2 = 1 - w0 - w1
            eps = -1e-4
            inside = (w0 >= eps) & (w1 >= eps) & (w2 >= eps) & (X >= 0) & (X < W) & (Y >= 0) & (Y < W)
            ti, gi = np.nonzero(inside)
            if len(ti) == 0:
                continue
            depth = (w0[ti, gi] * sz[t[ti], 0] + w1[ti, gi] * sz[t[ti], 1] + w2[ti, gi] * sz[t[ti], 2])
            pix = Y[ti, gi].astype(np.int64) * W + X[ti, gi].astype(np.int64)
            commit(pix, depth.astype(np.float32), shade[t[ti]])

    hit = np.isfinite(zbuf)
    # Subtle depth cue: farther surfaces slightly darker.
    if hit.any():
        zmin, zmax = zbuf[hit].min(), zbuf[hit].max()
        dz = np.where(hit, (zbuf - zmin) / max(zmax - zmin, 1e-6), 0)
        sbuf = np.where(hit, sbuf * (1.0 - 0.25 * dz), 0)
    rgb = np.clip(np.outer(sbuf, np.array(color, dtype=np.float32)), 0, 255)
    alpha = np.where(hit, 255, 0)
    img = np.concatenate([rgb, alpha[:, None]], axis=1).astype(np.uint8).reshape(W, W, 4)
    out = Image.fromarray(img, "RGBA")
    if supersample > 1:
        out = out.resize((size, size), Image.LANCZOS)
    return out


def render_stl_bytes(data: bytes, size: int = 512) -> bytes:
    img = render_triangles(parse_stl(data), size=size)
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=85, method=4)
    return buf.getvalue()


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def embedded_thumbnail(data: bytes, size: int = 512) -> bytes | None:
    """Pull the largest embedded PNG or JPEG out of a slicer file (LYS, CTX, ...)."""
    best: bytes | None = None
    pos = 0
    while (i := data.find(_PNG_SIG, pos)) != -1:
        j = data.find(b"IEND", i)
        if j == -1:
            break
        cand = data[i:j + 8]
        if best is None or len(cand) > len(best):
            best = cand
        pos = j + 8
    pos = 0
    while (i := data.find(b"\xff\xd8\xff", pos)) != -1:
        j = data.find(b"\xff\xd9", i + 3)
        if j == -1:
            break
        cand = data[i:j + 2]
        if best is None or len(cand) > len(best):
            best = cand
        pos = j + 2
    if not best or len(best) < 500:
        return None
    try:
        img = Image.open(io.BytesIO(best))
        img.load()
    except Exception:
        return None
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, "WEBP", quality=85)
    return buf.getvalue()


def image_thumbnail(data: bytes, size: int = 512) -> bytes:
    """Shrink a bundled preview picture (JPEG, PNG, WebP, ...) to a cached WebP."""
    img = Image.open(io.BytesIO(data))
    img.seek(0)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "WEBP", quality=85)
    return buf.getvalue()
