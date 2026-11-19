"""Turning a whole unit by a multiple of 90 degrees about the vertical axis.

The game only ever moves units (when lining portals up), never turns them, so
a copy of a unit that should connect to a wall facing another way has to be
turned in the data. This turns, about the origin:
* terrain vertices and morph data (material position and spectral delta);
* intros (position, spectral offset, facing) and their splines (positions and
  tangents);
* portals (triangles and box);
* VMObjects (position and, for moving ones, their per-vertex offsets);
then rebuilds every face normal from the turned geometry (normals follow the
faces' winding, as in all original data) and recompiles the BSP trees.

Not turned (reported when present): cameras, planning and sound markers,
point and spot lights, the light list's light directions (they only change how
objects are lit; the terrain's lighting is baked into vertex colours), and
signals other than stream signals.
"""

import math
import struct

import analyze
import bsp
from drm import Drm
from edit import NormalPool, _face_normal
from level import SIZEOF_INTRO, SIZEOF_NORMAL, SIZEOF_PORTAL, SIZEOF_TFACE, parse_level, signed16


def _rot(k):
    k %= 4
    return [lambda x, y: (x, y), lambda x, y: (-y, x), lambda x, y: (-x, -y), lambda x, y: (y, -x)][k]


def rotate(data, quarter_turns):
    """Return (new level bytes, [notes])."""
    k = quarter_turns % 4
    if k == 0:
        return data, ["no rotation"]
    R = _rot(k)
    L = parse_level(data)
    d = Drm(data)
    t = L.terrain
    notes = []

    def rot3(off, fmt="hhh"):
        x, y, z = d.unpack(fmt, off)
        nx, ny = R(x, y)
        d.pack(fmt, off, nx, ny, z)

    # terrain vertices, morph data
    vl = t.ptrs["vertexList"]
    for i in range(t.numVertices):
        rot3(vl + 12 * i)
    if t.ptrs["MorphDiffList"] is not None:
        o = t.ptrs["MorphDiffList"]
        for _ in t.morph_vertices:
            rot3(o)            # spectral delta
            rot3(o + 8)        # material position
            o += 14

    # intros and their splines
    for it in t.intros:
        rot3(it.offset + 0x20)
        rot3(it.offset + 0x44)
        rz = (d.s16(it.offset + 0x1C) + 1024 * k) % 4096
        d.pack("h", it.offset + 0x1C, rz - 4096 if rz >= 2048 else rz)
        if d.is_ptr(it.offset + 0x38):
            ms = d.ptr(it.offset + 0x38)
            for sp_off, kind in ((0x0, "positional"), (0x4, "rotational"), (0x8, "scaling"), (0xC, "colour")):
                if not d.is_ptr(ms + sp_off):
                    continue
                if kind != "positional":
                    notes.append("intro %s: %s spline not turned" % (it.name, kind))
                    continue
                spl = d.ptr(ms + sp_off)
                keys = d.ptr(spl) if d.is_ptr(spl) else None
                for i in range(d.s16(spl + 4) if keys is not None else 0):
                    ko = keys + 0x20 * i
                    rot3(ko + 2)
                    rot3(ko + 8, "iii")
                    rot3(ko + 0x14, "iii")

    # portals
    base = t.ptrs["StreamUnits"]
    for i in range(len(t.portals)):
        o = base + 4 + i * SIZEOF_PORTAL
        pts = []
        for j in range(3):
            rot3(o + 0x2C + 8 * j)
            rot3(o + 0x44 + 8 * j)
            pts += [d.unpack("hhh", o + 0x2C + 8 * j), d.unpack("hhh", o + 0x44 + 8 * j)]
        lo = [min(p[c] for p in pts) for c in range(3)]
        hi = [max(p[c] for p in pts) for c in range(3)]
        d.pack("hhh", o + 0x18, *lo)
        d.pack("hhh", o + 0x20, *hi)

    # VMObjects
    nvm = d.s32(0x08)
    if nvm:
        vmb = d.ptr(0x0C)
        for i in range(nvm):
            o = vmb + 0x3C * i
            rot3(o + 0x10)
            if d.u16(o) & 0x8 and d.is_ptr(o + 0x20):
                tl = d.ptr(o + 0x20)
                for j in range(d.s32(o + 0x1C)):
                    if not d.is_ptr(tl + 4 * j):
                        continue
                    tab = d.ptr(tl + 4 * j)
                    for m in range(d.s32(tab)):
                        rot3(tab + 4 + 6 * m)

    for name, off in (("cameras", 0x58), ("spot lights", 0x10), ("point lights", 0x18),
                      ("spectral spot lights", 0x20), ("spectral point lights", 0x28),
                      ("background objects", 0x30), ("planning markers", 0xE0), ("sound markers", 0xE8)):
        if d.s32(off):
            notes.append("%d %s not turned" % (d.s32(off), name))

    # normals, rebuilt from the turned faces
    L2 = parse_level(d.to_bytes())
    t2 = L2.terrain
    mat = analyze.material_positions(L2)
    spec = analyze.spectral_positions(L2)
    pool = NormalPool([(0, 0, 0, 2)])
    old_pool = t.normals
    face_n, morph_n = [], []

    def turned_old(idx):
        n = old_pool[abs(idx)]
        s = -1 if idx < 0 else 1
        x, y = R(n[0] * s, n[1] * s)
        v = (x, y, n[2] * s)
        ln = math.sqrt(sum(c * c for c in v)) or 1.0
        return tuple(c / ln for c in v)

    for i, f in enumerate(t2.faces):
        n = _face_normal(mat, f)
        face_n.append(pool.index(n if n is not None else turned_old(signed16(f.normal))))
        if t2.morph_normals:
            n = _face_normal(spec, f)
            morph_n.append(pool.index(n if n is not None else turned_old(signed16(t2.morph_normals[i]))))
    from level import encode_normal
    nl = t2.ptrs["normalList"]
    old_len = SIZEOF_NORMAL * t2.numNormals
    blob = b"".join(struct.pack("<hhh", *encode_normal(*e)) if i else b"\0" * 6 for i, e in enumerate(pool.pool))
    while (len(blob) - old_len) % 4:
        blob += b"\0\0"
    d = Drm(d.to_bytes())
    if (old_len % 4) or (nl % 4):
        a, bnd = nl & ~3, (nl + old_len + 3) & ~3
        head = bytes(d.body[a:nl])
        tail = bytes(d.body[nl + old_len:bnd])
        d.replace(a, bnd, head + blob + tail, allow_into=[d.ptr(0) + 0x24])
    else:
        d.replace(nl, nl + old_len, blob)
    t_off = d.ptr(0)
    d.pack("i", t_off + 0x18, len(pool.pool))
    fl = d.ptr(t_off + 0x20)
    for i, n in enumerate(face_n):
        d.pack("h", fl + i * SIZEOF_TFACE + 8, n)
    if morph_n:
        mo = d.ptr(t_off + 0x4C)
        for i, n in enumerate(morph_n):
            d.pack("h", mo + 2 * i, n)

    out, _ = bsp.rebuild_level(d.to_bytes())
    notes.insert(0, "turned by %d degrees: %d vertices, %d intros, %d portals, %d VMObjects; %d normals, "
                    "BSP trees recompiled" % (90 * k, t.numVertices, t.numIntros, len(t.portals), nvm,
                                              len(pool.pool)))
    return out, notes


def turn_points(points, quarter_turns):
    R = _rot(quarter_turns)
    return [tuple(R(p[0], p[1])) + (p[2],) for p in points]
