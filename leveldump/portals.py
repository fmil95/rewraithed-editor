"""Moving and adding portals (see units.py for how portals work).

A portal is a StreamUnitPortal record (t1, t2: the two triangles of its quad;
min, max: their bounding box) plus two trigger faces in the tree with ID -1
whose corners are exactly t1 and t2, and whose MultiSignal holds a stream
signal with the same destination.

When a neighbour streams in, the game moves it so that its portal's `min`
lands on the matching portal's `min` (STREAM_ConnectStream:
offset = main.min - neighbour.min; RelocateLevel). Units are only moved, never
turned, so the portals on both sides must have the same size and orientation:
only their position may differ. That is what lets a unit connect somewhere
else: move (or add) the portal on one side, and the unit on the other side
follows.
"""

import struct

import geometry
from drm import Drm
from edit import LevelEditor
from level import SIZEOF_PORTAL, parse_level
import units


def portal_quads(level):
    """[(portal index, record offset, [t1 corners], [t2 corners], [trigger face indices])]."""
    t = level.terrain
    d = level.drm
    base = t.ptrs["StreamUnits"]
    sig = units.signal_faces(level)
    out = []
    for i, p in enumerate(t.portals):
        faces = []
        corners = set(map(tuple, p.t1)) | set(map(tuple, p.t2))
        for fi, msig in sig:
            for sp in units.stream_signals(d, msig):
                cur = d.s32(sp + 4)
                if cur == p.MSignalID and d.cstr(sp + 12, 16) == p.tolevelname:
                    f = t.faces[fi]
                    if {(t.vertices[v].x, t.vertices[v].y, t.vertices[v].z) for v in (f.v0, f.v1, f.v2)} <= corners:
                        faces.append(fi)
        out.append((i, base + 4 + i * SIZEOF_PORTAL, [tuple(c) for c in p.t1], [tuple(c) for c in p.t2], faces))
    return out


def _write_quad(d, rec, t1, t2):
    pts = list(t1) + list(t2)
    lo = tuple(min(p[k] for p in pts) for k in range(3))
    hi = tuple(max(p[k] for p in pts) for k in range(3))
    flags = d.s16(rec + 0x1E)
    d.pack("hhhh", rec + 0x18, lo[0], lo[1], lo[2], flags)
    pad = d.s16(rec + 0x26)
    d.pack("hhhh", rec + 0x20, hi[0], hi[1], hi[2], pad)
    for k, p in enumerate(t1):
        d.pack("hhh", rec + 0x2C + 8 * k, *p)
    for k, p in enumerate(t2):
        d.pack("hhh", rec + 0x44 + 8 * k, *p)


def reshape(data, index, t1, t2):
    """Give portal `index` a new quad (t1, t2: three corners each), moving its
    trigger faces along. Returns new bytes (trigger-face bounds rebuilt)."""
    L = parse_level(data)
    quads = portal_quads(L)
    i, rec, old1, old2, faces = quads[index]
    move = {}
    for a, b in zip(old1 + old2, list(t1) + list(t2)):
        move.setdefault(a, tuple(int(c) for c in b))
    ed = LevelEditor(data)
    t = ed.level.terrain
    for fi in faces:
        f = t.faces[fi]
        for v in (f.v0, f.v1, f.v2):
            p = (t.vertices[v].x, t.vertices[v].y, t.vertices[v].z)
            if p in move:
                ed.move_vertex(v, material=move[p])
    out = ed.build()
    d = Drm(out)
    rec = parse_level(out).terrain.ptrs["StreamUnits"] + 4 + index * SIZEOF_PORTAL
    _write_quad(d, rec, t1, t2)
    return d.to_bytes(), "portal %d -> %s: quad %s, %d trigger faces moved" % (
        index, L.terrain.portals[index].tolevelname, (t1, t2), len(faces))


def move(data, index, delta):
    L = parse_level(data)
    _, _, t1, t2, _ = portal_quads(L)[index]
    sh = lambda ps: [tuple(p[k] + delta[k] for k in range(3)) for p in ps]  # noqa: E731
    return reshape(data, index, sh(t1), sh(t2))


def add(data, dest, dest_num, t1, t2, own_num=None):
    """A new portal to unit `dest`, arriving at its portal number `dest_num`.
    Returns (new bytes, own portal number). The other unit needs a portal
    back with tolevelname "<this unit>,<own number>"."""
    L = parse_level(data)
    t = L.terrain
    d = Drm(data)
    if own_num is None:
        used = {p.MSignalID for p in t.portals}
        for _, msig in units.signal_faces(L):
            used.add(d.s16(msig + 4))
        own_num = max(used | {0}) + 1
    name = "%s,%d" % (dest, dest_num)
    dest_id = units.disc.hash_unit(dest)
    pts = list(t1) + list(t2)
    lo = tuple(min(p[k] for p in pts) for k in range(3))
    hi = tuple(max(p[k] for p in pts) for k in range(3))

    # 1. the record, appended to Terrain.StreamUnits
    rec = bytearray(SIZEOF_PORTAL)
    nb = name.encode("latin-1")
    rec[0:len(nb)] = nb
    struct.pack_into("<ii", rec, 0x10, own_num, dest_id)
    struct.pack_into("<hhhh", rec, 0x18, lo[0], lo[1], lo[2], 0)
    struct.pack_into("<hhhh", rec, 0x20, hi[0], hi[1], hi[2], 0)
    for k, p in enumerate(t1):
        struct.pack_into("<hhh", rec, 0x2C + 8 * k, *p)
    for k, p in enumerate(t2):
        struct.pack_into("<hhh", rec, 0x44 + 8 * k, *p)
    base = t.ptrs["StreamUnits"]
    n = len(t.portals)
    d.insert(base + 4 + n * SIZEOF_PORTAL, SIZEOF_PORTAL)
    base = d.ptr(d.ptr(0) + 0x30)
    d.body[base + 4 + n * SIZEOF_PORTAL: base + 4 + (n + 1) * SIZEOF_PORTAL] = rec
    d.pack("i", base, n + 1)

    # 2. its MultiSignal, appended at Level.SignalListEnd: one stream signal, then the end
    msig = struct.pack("<ihh", 1, own_num, 0) + struct.pack("<iii", units.SIGNAL_STREAM, own_num, dest_id) + \
        nb + b"\0" * (16 - len(nb)) + struct.pack("<i", units.SIGNAL_END)
    end = d.ptr(0xD4)
    d.insert(end, len(msig))               # SignalListEnd (== end) moves to the new end
    d.body[end:end + len(msig)] = msig
    textoff = end - d.ptr(d.ptr(0) + 0x50)
    if not 0 <= textoff < 0xFFFF:
        raise SystemExit("the new signal is out of reach of the trigger faces' 16-bit offset")

    # 3. the trigger faces, in the tree with ID -1, then a BSP rebuild
    L2 = parse_level(d.to_bytes())
    trig = [tr.index for tr in L2.terrain.trees if tr.ID == -1]
    if not trig:
        raise SystemExit("this unit has no trigger tree (ID -1)")
    te = geometry.TopologyEdit(d.to_bytes()).from_level()
    te.add_faces(trig[0], [t1, t2], 0x44, textoff)
    out, info = te.build()
    return out, own_num
