#!/usr/bin/env python3
"""A new unit built from nothing: an empty level container plus generated geometry.

The container (Level and Terrain headers, light list, fog, signal list, the
portal record that leads to the unit it connects to) comes from an existing
unit, with all of its content removed: vertices, faces, morph data, intros,
animated objects, animated textures, event scripts and its other portals. Its
own texture entries and normals stay in the file unused. Then a room is
generated:

* a box room (floor, ceiling, four walls) facing inwards, tiled with the
  texture entries of the unit it connects to (a floor texture and a wall
  texture), so it shares that unit's video memory: the game draws a neighbour
  with the current unit's video memory, and the new unit's .crm is a copy of
  the connected unit's;
* an opening in one wall exactly where the connecting portal is, with the
  portal's two trigger faces in the trigger tree (ID -1);
* a Raziel start inside;
then the BSP trees are compiled.

    python newunit.py --disc KAIN2.img rewr1 --connect city9 --wall-y 0 \\
        --door 10273 -646 10909 1267 --install <exe folder>/mods

The doorway is cut into a south-facing wall (constant y) of --connect, which
gets a new portal to the new unit; the new unit uses the same coordinates, so
the game lines the two up with no offset. Nothing else in --connect changes,
and no other unit is touched (the container's own unit keeps its files).
"""

import argparse
import collections
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze  # noqa: E402
import disc  # noqa: E402
import geometry  # noqa: E402
import intros as intromod  # noqa: E402
import portals  # noqa: E402
import textures  # noqa: E402
import units  # noqa: E402
import vrambudget  # noqa: E402
from drm import Drm  # noqa: E402
from level import SIZEOF_MORPHVERTEX, SIZEOF_TFACE, SIZEOF_TVERTEX, parse_level  # noqa: E402


def _replace_words(d, start, old_len, new, allow=()):
    """Replace body[start:start+old_len] by `new`, keeping 4-byte alignment of
    everything behind it (pads `new` so the size changes by a multiple of 4,
    and swaps whole words when the range isn't word-aligned)."""
    import struct as _st
    new = bytes(new)
    while (len(new) - old_len) % 4:
        new += b"\0"
    a = start & ~3
    end = start + old_len
    b = (end + 3) & ~3
    head = bytes(d.body[a:start])
    tail = bytes(d.body[end:b])
    # pointers to the data right behind the range (e.g. the next list) can
    # fall inside the word-aligned window: let them through, then point them
    # at the tail's new place
    to_end = [r for r in d.reloc_order if _st.unpack_from("<I", d.body, r)[0] == end and a < end < b]
    delta = d.replace(a, b, head + new + tail, allow_into=list(allow) + to_end)
    for r in to_end:
        loc = r + delta if r >= b else r
        d.pack("I", loc, a + len(head) + len(new))


def strip(data):
    """Remove the unit's geometry and content, keeping the container."""
    L = parse_level(data)
    d = Drm(data)
    t = L.terrain
    tp = lambda off: d.ptr(d.ptr(0) + off)  # noqa: E731
    terr = lambda: d.ptr(0)  # noqa: E731
    # The leaves are rebuilt later; until then their face pointers all point at
    # the start of the face list, so they stay valid while everything moves.
    fl0 = tp(0x20)
    for tr in t.trees:
        for lf in tr.leaves:
            if d.is_ptr(lf.offset + 8):
                d.pack("I", lf.offset + 8, fl0)

    # face list and spectral normal index
    _replace_words(d, tp(0x20), SIZEOF_TFACE * t.numFaces, b"", allow=[terr() + 0x20])
    if tp(0x4C) is not None:
        _replace_words(d, tp(0x4C), 2 * t.numFaces, b"", allow=[terr() + 0x4C])
    # morph list: only its end entry
    if tp(0x3C) is not None:
        mdl = tp(0x3C)
        n = len(t.morph_vertices)
        end_entry = bytes(d.body[mdl + n * SIZEOF_MORPHVERTEX: mdl + (n + 1) * SIZEOF_MORPHVERTEX])
        _replace_words(d, mdl, (n + 1) * SIZEOF_MORPHVERTEX, end_entry, allow=[terr() + 0x3C])
    # spectral colours, vertices
    if tp(0x40) is not None:
        _replace_words(d, tp(0x40), 2 * t.numVertices, b"", allow=[terr() + 0x40])
    _replace_words(d, tp(0x1C), SIZEOF_TVERTEX * t.numVertices, b"", allow=[terr() + 0x1C])
    d.pack("i", terr() + 0x10, 0)
    d.pack("i", terr() + 0x14, 0)
    # no animated objects, animated textures or event scripts
    d.pack("i", 0x08, 0)
    if tp(0x28) is not None:
        d.pack("i", tp(0x28), 0)
    if d.is_ptr(0xDC):
        d.pack("i", d.ptr(0xDC), 0)
    return d.to_bytes()


def texture_from(level, want_up):
    """The most used texture entry on the unit's floors (want_up) or walls,
    as (entry, (u0, v0, u1, v1)) with the UV rectangle of that entry."""
    t = level.terrain
    entries = textures.texture_entries(level)
    pos = analyze.material_positions(level)
    from edit import _face_normal
    use = collections.Counter()
    for f in t.faces:
        tex = textures.face_texture(level, entries, f)
        n = _face_normal(pos, f)
        if tex is None or n is None:
            continue
        if (n[2] > 0.9) == want_up and (want_up or abs(n[2]) < 0.2):
            use[f.textoff // 12] += 1
    e = entries[use.most_common(1)[0][0]]
    us, vs = (e[0], e[3], e[6]), (e[1], e[4], e[7])
    return e, (min(us), min(vs), max(us), max(vs))


def _tri_entry(base, uvs):
    """A TextureFT3 entry like `base` with the corner UVs `uvs` (3 pairs)."""
    return (uvs[0][0], uvs[0][1], base[2], uvs[1][0], uvs[1][1], base[5], uvs[2][0], uvs[2][1], base[8])


def quad(a, b, c, d, normal, tex, rect):
    """Two triangles for the quad a-b-c-d (corners in order around it), wound
    so their normal points along `normal`, with the texture rectangle on it."""
    u0, v0, u1, v1 = rect
    uv = {0: (u0, v1), 1: (u1, v1), 2: (u1, v0), 3: (u0, v0)}
    pts = [a, b, c, d]
    e1 = [pts[1][k] - pts[0][k] for k in range(3)]
    e2 = [pts[2][k] - pts[0][k] for k in range(3)]
    n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
    order = [0, 1, 2, 3] if sum(n[k] * normal[k] for k in range(3)) > 0 else [0, 3, 2, 1]
    tris, ents = [], []
    for tri in ((order[0], order[1], order[2]), (order[0], order[2], order[3])):
        tris.append([pts[i] for i in tri])
        ents.append(_tri_entry(tex, [uv[i] for i in tri]))
    return tris, ents


def grid(p0, du, dv, nu, nv, normal, tex, rect):
    """A surface p0 + i*du + j*dv tiled nu x nv, one texture rectangle per tile."""
    tris, ents = [], []
    for i in range(nu):
        for j in range(nv):
            c = [tuple(p0[k] + (i + a) * du[k] / nu + (j + b) * dv[k] / nv for k in range(3))
                 for a, b in ((0, 0), (1, 0), (1, 1), (0, 1))]
            t_, e_ = quad(*c, normal, tex, rect)
            tris += t_
            ents += e_
    return tris, ents


def room_around_doorway(t1, t2, depth=5000, width=6100, height=3000, tile=1000):
    """Room geometry behind a doorway in a wall of constant y (the portal's
    plane), the room extending towards +y. Returns {surface: (corner, du, dv)}
    plus the wall pieces around the opening."""
    pts = list(t1) + list(t2)
    y = min(p[1] for p in pts)
    zb = min(p[2] for p in pts)
    zt = max(p[2] for p in pts)
    bottom = sorted({p[0] for p in pts if p[2] == zb})
    top = sorted({p[0] for p in pts if p[2] == zt})
    cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
    x0, x1 = int(cx - width / 2), int(cx + width / 2)
    y1 = y + depth
    z1 = zb + height
    return dict(y=y, y1=y1, x0=x0, x1=x1, zb=zb, zt=zt, z1=z1, bottom=bottom, top=top)


def build_room(r, floor_tex, wall_tex, tile=1000):
    tris, ents = [], []

    def add(t_e):
        tris.extend(t_e[0])
        ents.extend(t_e[1])

    x0, x1, y, y1, zb, z1, zt = r["x0"], r["x1"], r["y"], r["y1"], r["zb"], r["z1"], r["zt"]
    nx, ny, nz = max(1, (x1 - x0) // tile), max(1, (y1 - y) // tile), max(1, (z1 - zb) // tile)
    ft, fr = floor_tex
    wt, wr = wall_tex
    add(grid((x0, y, zb), (x1 - x0, 0, 0), (0, y1 - y, 0), nx, ny, (0, 0, 1), ft, fr))        # floor
    add(grid((x0, y, z1), (x1 - x0, 0, 0), (0, y1 - y, 0), nx, ny, (0, 0, -1), ft, fr))       # ceiling
    add(grid((x0, y1, zb), (x1 - x0, 0, 0), (0, 0, z1 - zb), nx, nz, (0, -1, 0), wt, wr))     # north wall
    add(grid((x0, y, zb), (0, y1 - y, 0), (0, 0, z1 - zb), ny, nz, (1, 0, 0), wt, wr))        # west wall
    add(grid((x1, y, zb), (0, y1 - y, 0), (0, 0, z1 - zb), ny, nz, (-1, 0, 0), wt, wr))       # east wall
    # south wall, around the opening (a trapezoid: bottom and top edges of the doorway)
    bl, br = r["bottom"][0], r["bottom"][-1]
    tl, tr = r["top"][0], r["top"][-1]
    add(quad((x0, y, zb), (bl, y, zb), (tl, y, zt), (x0, y, zt), (0, 1, 0), wt, wr))           # left of it
    add(quad((br, y, zb), (x1, y, zb), (x1, y, zt), (tr, y, zt), (0, 1, 0), wt, wr))           # right of it
    add(grid((x0, y, zt), (x1 - x0, 0, 0), (0, 0, z1 - zt), nx, max(1, (z1 - zt) // tile), (0, 1, 0), wt, wr))
    return tris, ents


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--disc", required=True)
    ap.add_argument("name", help="the new unit: an area name and a number, e.g. rewr1")
    ap.add_argument("--connect", required=True, help="the existing unit it opens onto, e.g. city9")
    ap.add_argument("--wall-y", type=int, required=True, help="the y of the connected unit's wall (a south-facing wall)")
    ap.add_argument("--door", nargs=4, type=int, required=True, metavar=("X0", "Z0", "X1", "Z1"),
                    help="the doorway in that wall; its wall faces inside are removed")
    ap.add_argument("--container", default="city19",
                    help="an existing unit whose level file is emptied and reused as the container")
    ap.add_argument("--install", required=True, metavar="MODS_DIR")
    ap.add_argument("--depth", type=int, default=5000)
    ap.add_argument("--width", type=int, default=4000)
    ap.add_argument("--height", type=int, default=3000)
    args = ap.parse_args(argv)

    bf = disc.BigFile(args.disc)
    units.check_new_name(bf, args.name)
    mods = args.install

    def read_unit(u):
        p = units.unit_path(u, "drm", mods)
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
        return bf.read_level(u)

    x0, z0, x1, z1 = args.door
    y = args.wall_y
    # connected side: the trigger's front faces the connected unit (towards -y)
    door_c = [(x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1)]
    c_t1, c_t2 = [door_c[0], door_c[1], door_c[2]], [door_c[0], door_c[2], door_c[3]]
    # room side: front faces the room (towards +y)
    r_t1, r_t2 = [door_c[0], door_c[3], door_c[2]], [door_c[0], door_c[2], door_c[1]]

    # --- the container ------------------------------------------------------
    data = bf.read_level(args.container)     # the disc's version; the container's own unit keeps its files
    cont = parse_level(data)
    keep = cont.terrain.portals[0]
    for p in cont.terrain.portals[1:]:
        data, msg = units.disable(data, p.dest_unit)
    own_num = keep.MSignalID
    connect_data = read_unit(args.connect)
    connect = parse_level(connect_data)
    code = collections.Counter(v.code for v in connect.terrain.vertices).most_common(1)[0][0]
    mat_col = tuple(sum(c) // len(connect.terrain.vertices)
                    for c in zip(*[(v.r0, v.g0, v.b0) for v in connect.terrain.vertices]))
    spec_col = tuple(sum(c) // len(connect.terrain.morph_colors) for c in zip(*[
        ((c & 31) << 3, (c >> 2) & 0xF8, (c >> 7) & 0xF8) for c in connect.terrain.morph_colors]))

    # --- the connected unit: a doorway and a portal to the new unit ---------
    cdata, msg = units.cut_opening(connect_data, 0, [x0 - 10, y - 5, z0 - 10, x1 + 10, y + 5, z1 + 10], inside=True)
    print("%s: %s" % (args.connect, msg))
    cdata, c_num = portals.add(cdata, args.name, own_num, c_t1, c_t2)
    print("%s: portal %d -> %s,%d" % (args.connect, c_num, args.name, own_num))

    # the container's remaining portal: this doorway, leading to the connected unit's new portal
    data, _ = units.retarget(data, keep.dest_unit, args.connect, c_num)
    L = parse_level(data)
    d = Drm(data)
    portals._write_quad(d, L.terrain.ptrs["StreamUnits"] + 4, r_t1, r_t2)
    data = d.to_bytes()
    L = parse_level(data)
    sig_textoff = None
    for fi, ms in units.signal_faces(L):
        if units.stream_signals(L.drm, ms):
            sig_textoff = L.terrain.faces[fi].textoff
    data = strip(data)
    print("container: %s emptied (%d bytes)" % (args.container, len(data)))

    # --- the room -----------------------------------------------------------
    r = dict(y=y, y1=y + args.depth, x0=(x0 + x1) // 2 - args.width // 2, x1=(x0 + x1) // 2 + args.width // 2,
             zb=z0, zt=z1, z1=z0 + args.height, bottom=[x0, x1], top=[x0, x1])
    floor_tex = texture_from(connect, True)
    wall_tex = texture_from(connect, False)
    tris, ents = build_room(r, floor_tex, wall_tex)
    te = geometry.TopologyEdit(data).from_level()
    trees = te.L.terrain.trees
    geo_tree = [tr.index for tr in trees if tr.ID == 0][0]
    trig_tree = [tr.index for tr in trees if tr.ID == -1][0]
    te.add_faces(geo_tree, tris, 0, None, colour=mat_col, spectral_colour=spec_col, code=code, textures=ents)
    te.add_faces(trig_tree, [r_t1, r_t2], 0x44, sig_textoff, code=code)
    out, info = te.build()
    print("room: %d faces, %d vertices; trees %s" % (len(tris), len(te.new_verts),
          [(s["tree"], s["faces"], s["leaves"]) for s in info["trees"]]))
    out, notes = units.rename(out, args.container, args.name)
    for n in notes:
        print(n)

    # a Raziel start in the room, facing away from the doorway (rotation 0 = +y)
    tmp = os.path.join(os.path.dirname(os.path.abspath(mods)), "_newunit_intros.obj")
    start = ((x0 + x1) // 2, y + 1200, z0)
    with open(tmp, "w", newline="\n") as f:
        f.write("\n".join(intromod.arrow_obj("intro_new_raziel", start, 0, 0.01, 1)) + "\n")
    out, lines, _ = intromod.apply_intros(out, tmp, 0.01, [connect])
    os.remove(tmp)
    for line in lines:
        print(line)

    # --- checks, then write -------------------------------------------------
    import bspbuild
    for label, blob in ((args.name, out), (args.connect, cdata)):
        _, bad, misses = bspbuild.check(blob, 300, label)
        if bad or misses:
            raise SystemExit("not written")
    dst = units.unit_path(args.name, "drm", mods)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(out)
    print("wrote", dst)
    crm = bf.read_file(disc.level_path(args.connect).replace(".drm", ".crm"), args.connect)
    with open(units.unit_path(args.name, "crm", mods), "wb") as f:
        f.write(crm)
    print("wrote", units.unit_path(args.name, "crm", mods), "(textures: %s's)" % args.connect)
    p = units.unit_path(args.connect, "drm", mods)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(cdata)
    print("wrote", p)
    units_txt = os.path.join(mods, "units.txt")
    lines = []
    if os.path.exists(units_txt):
        with open(units_txt) as f:
            lines = [ln for ln in f if ln.split("=")[0].strip().lower() != args.name.lower()]
    lines.append("%s = %s\n" % (args.name, args.container))
    with open(units_txt, "w") as f:
        f.writelines(lines)
    print("wrote", units_txt)
    vrambudget.check_files(args.disc, mods, [args.name, args.connect])
    return 0


if __name__ == "__main__":
    sys.exit(main())
