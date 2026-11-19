#!/usr/bin/env python3
"""Portals and new units.

How a portal works (see README "Portals and units"):
* Terrain.StreamUnits lists StreamUnitPortal records: "<unit>,<n>" (the unit
  on the other side and the number of the portal there), this portal's own
  number (MSignalID), the other unit's stream ID, a box and two triangles. The
  streaming code loads the neighbour from it and lines the two units up
  (STREAM_ConnectStream: offset = this.min - other.min of the matching pair).
* The crossing itself is two terrain faces in the tree with ID -1, face attr
  0x44. For those faces TFace.textoff is not a texture but an offset into
  Terrain.signals, a MultiSignal {long numSignals; short signalNum; short
  flags; signals...}; crossing a face fires it (COLLIDE_LineWithSignals). A
  portal's MultiSignal holds signal 18, "stream level": {currentnum, streamID,
  toname[16]}, the same data as the portal record. Signal lists end with 15.
* A unit's stream ID is LOAD_HashUnit(name); its files are
  \\kain2\\area\\<area>\\bin\\<unit>.drm/.crm (.snf/.smf sound, optional), and the
  game needs a bigfile directory with that ID (the port adds one for units
  listed in mods\\units.txt).

    python units.py --disc KAIN2.img new under2 under5 --tint 255 120 120 \\
        --disable-portal under4 --install <exe folder>/mods
    python units.py --disc KAIN2.img retarget under1 under2 under5 --install <exe folder>/mods
    python units.py --disc KAIN2.img move-portal under1 0 -4000 0 0 --install <exe folder>/mods
    python units.py --disc KAIN2.img add-portal under1 under6 2 --like under2:0 --at -3000 -2500 0 \
        --install <exe folder>/mods
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import disc  # noqa: E402
from drm import Drm  # noqa: E402
from level import SIZEOF_PORTAL, parse_level  # noqa: E402
import vrambudget  # noqa: E402

SIGNAL_END = 15
SIGNAL_STREAM = 18
# payload length in words per signal id (signalInfoList in SIGNAL.c)
SIGNAL_LENGTH = [1, 1, 1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 2, 1, 0, 0, 0, 6, 2, 1, 1, 1, 1, 0, 1, 1]


def signal_faces(level):
    """[(face index, MultiSignal body offset)] of the faces that fire signals."""
    t = level.terrain
    base = t.ptrs["signals"]
    out = []
    if base is None:
        return out
    for i, f in enumerate(t.faces):
        if f.attr & 0xC0 and f.textoff != 0xFFFF:
            out.append((i, base + f.textoff))
    return out


def stream_signals(d, msig):
    """Body offsets of the stream-level signals (id 18) in a MultiSignal."""
    n = d.s32(msig)
    p = msig + 8
    out = []
    for _ in range(n):
        while True:
            sid = d.s32(p) & 0x7FFFFFFF
            if sid == SIGNAL_END:
                break
            if sid >= len(SIGNAL_LENGTH):
                return out          # unknown signal: stop rather than guess
            if sid == SIGNAL_STREAM:
                out.append(p)
            p += 4 + 4 * SIGNAL_LENGTH[sid]
        break
    return out


def _set_name(d, off, name):
    b = name.encode("latin-1")
    if len(b) > 15:
        raise SystemExit("%r is too long (15 characters at most)" % name)
    d.body[off:off + 16] = b + b"\0" * (16 - len(b))


def retarget(data, old, new, number=None):
    """Point every portal (record and signal) of a unit that leads to `old` at
    `new` (and, with `number`, at that portal number there)."""
    L = parse_level(data)
    d = Drm(data)
    t = L.terrain
    new_id = disc.hash_unit(new)
    n = 0
    base = t.ptrs["StreamUnits"]
    for i, p in enumerate(t.portals):
        if p.dest_unit.lower() == old.lower():
            o = base + 4 + i * SIZEOF_PORTAL
            rest = p.tolevelname[len(p.dest_unit):] if number is None else ",%d" % number
            _set_name(d, o, new + rest)
            d.pack("i", o + 0x14, new_id)
            n += 1
    s = 0
    for _, msig in signal_faces(L):
        for sp in stream_signals(d, msig):
            name = d.cstr(sp + 12, 16)
            if name.split(",")[0].lower() == old.lower():
                rest = name[len(name.split(",")[0]):] if number is None else ",%d" % number
                _set_name(d, sp + 12, new + rest)
                d.pack("i", sp + 8, new_id)
                s += 1
    return d.to_bytes(), "%d portal records and %d stream signals now lead to %s" % (n, s // 1, new)


def disable(data, dest):
    """Remove the portal records to `dest` and empty their crossing signals."""
    L = parse_level(data)
    d = Drm(data)
    t = L.terrain
    base = t.ptrs["StreamUnits"]
    keep = [i for i, p in enumerate(t.portals) if p.dest_unit.lower() != dest.lower()]
    removed = len(t.portals) - len(keep)
    recs = [bytes(d.body[base + 4 + i * SIZEOF_PORTAL: base + 4 + (i + 1) * SIZEOF_PORTAL]) for i in keep]
    for j, r in enumerate(recs):
        d.body[base + 4 + j * SIZEOF_PORTAL: base + 4 + (j + 1) * SIZEOF_PORTAL] = r
    d.pack("i", base, len(keep))
    targets = set()
    for _, msig in signal_faces(L):
        for sp in stream_signals(d, msig):
            if d.cstr(sp + 12, 16).split(",")[0].lower() == dest.lower():
                targets.add(msig)
    data = d.to_bytes()
    # empty them from the last to the first, so earlier offsets stay valid
    for msig in sorted(targets, reverse=True):
        data = empty_multisignal(data, msig)
    return data, "removed %d portal record(s) to %s, emptied %d signal(s)" % (removed, dest, len(targets))


def walk_signals(d):
    """[(start, end)] of the MultiSignals from Level.SignalListStart to
    SignalListEnd, the way SIGNAL_RelocateSignal walks them (header, numSignals
    signals, a 4-byte end marker id 15, the next one right after). Raises
    ValueError where the list doesn't walk."""
    s, e = d.ptr(0xD0), d.ptr(0xD4)
    out = []
    m = s
    while m < e:
        n = d.s32(m)
        p = m + 8
        for _ in range(n):
            sid = d.s32(p) & 0x7FFFFFFF
            if sid >= len(SIGNAL_LENGTH):
                raise ValueError("unknown signal id %d at 0x%x" % (sid, p))
            p += 4 + 4 * SIGNAL_LENGTH[sid]
        if d.s32(p) & 0x7FFFFFFF != SIGNAL_END:
            raise ValueError("MultiSignal at 0x%x has no end marker after its %d signals" % (m, n))
        out.append((m, p + 4))
        m = p + 4
    if m != e:
        raise ValueError("the signal list runs past SignalListEnd")
    return out


def empty_multisignal(data, msig):
    """Make the MultiSignal at `msig` do nothing: shrink it to its header with
    no signals and the end marker (12 bytes, like the empty ones in the game),
    then fix the trigger faces whose signal offsets lie behind it. Setting only
    numSignals to 0 would leave its old signals where the engine expects the
    next MultiSignal, and SIGNAL_RelocateSignal walks into garbage."""
    L = parse_level(data)
    d = Drm(data)
    t = L.terrain
    base = t.ptrs["signals"]
    p = msig + 8
    while d.s32(p) & 0x7FFFFFFF != SIGNAL_END:        # to its end marker, whatever numSignals says
        sid = d.s32(p) & 0x7FFFFFFF
        if sid >= len(SIGNAL_LENGTH):
            raise SystemExit("can't walk the MultiSignal at 0x%x (signal id %d)" % (msig, sid))
        p += 4 + 4 * SIGNAL_LENGTH[sid]
    old_end = p + 4
    new = struct.pack("<ihhi", 0, d.s16(msig + 4), d.s16(msig + 6), SIGNAL_END)
    delta = d.replace(msig, old_end, new)
    if delta:
        fl = d.ptr(d.ptr(0) + 0x20)
        for i, f in enumerate(t.faces):
            if f.attr & 0xC0 and f.textoff != 0xFFFF and base + f.textoff >= old_end:
                d.pack("H", fl + 12 * i + 0xA, f.textoff + delta)
    return d.to_bytes()


def repair_signals(data):
    """Fix MultiSignals emptied by an older version of `disable` (numSignals 0
    but the old signals still in place)."""
    fixed = 0
    while True:
        L = parse_level(data)
        d = L.drm
        s, e = d.ptr(0xD0), d.ptr(0xD4)
        m, bad = s, None
        while m < e:
            n = d.s32(m)
            p = m + 8
            for _ in range(n):
                p += 4 + 4 * SIGNAL_LENGTH[d.s32(p) & 0x7FFFFFFF]
            if d.s32(p) & 0x7FFFFFFF != SIGNAL_END:
                bad = m
                break
            m = p + 4
        if bad is None:
            return data, fixed
        data = empty_multisignal(data, bad)
        fixed += 1


def rename(data, old, new):
    """A copy of unit `old` that is unit `new`: name, stream ID, monsters' valid units."""
    L = parse_level(data)
    d = Drm(data)
    old_id, new_id = L.streamUnitID, disc.hash_unit(new)
    notes = []
    wn = d.ptr(0x98)
    if len(new) <= len(L.worldName):
        _set_name_inplace(d, wn, new, len(L.worldName))
    else:
        d.body += b"\0" * (-len(d.body) % 4)
        off = len(d.body)
        d.body += new.encode("latin-1") + b"\0" * (4 - len(new) % 4)
        d.pack("I", 0x98, off)
    d.pack("i", 0xF8, new_id)
    # monsters' valid-units lists (intro command 14)
    fixed = 0
    for it in L.terrain.intros:
        if not it.has_data:
            continue
        p = d.ptr(it.offset + 0x30)
        while True:
            cmd, n = d.unpack("hh", p)
            if cmd == 0:
                break
            if cmd == 14:
                for k in range(n):
                    if (d.s32(p + 4 + 4 * k) & 0xFFFF) == (old_id & 0xFFFF):
                        d.pack("i", p + 4 + 4 * k, new_id)
                        fixed += 1
            p += 4 + 4 * n
    notes.append("name %s -> %s, stream ID %d -> %d, %d valid-unit entries updated" % (
        L.worldName, new, old_id, new_id, fixed))
    return d.to_bytes(), notes


def _set_name_inplace(d, off, name, old_len):
    b = name.encode("latin-1")
    d.body[off:off + old_len + 1] = b + b"\0" * (old_len + 1 - len(b))


def tint(data, rgb):
    """Multiply every vertex colour (both realms) by rgb / 255."""
    L = parse_level(data)
    d = Drm(data)
    t = L.terrain

    def mul15(c):
        r, g, b = c & 31, (c >> 5) & 31, (c >> 10) & 31
        r, g, b = (min(31, v * k // 255) for v, k in zip((r, g, b), rgb))
        return r | (g << 5) | (b << 10) or 0x0421

    vl = t.ptrs["vertexList"]
    for i, v in enumerate(t.vertices):
        o = vl + i * 12
        d.pack("H", o + 6, mul15(v.rgb15))
        d.pack("BBB", o + 8, *(min(255, c * k // 255) for c, k in zip((v.r0, v.g0, v.b0), rgb)))
    if t.ptrs["MorphColorList"] is not None:
        for i, c in enumerate(t.morph_colors):
            d.pack("H", t.ptrs["MorphColorList"] + 2 * i, mul15(c))
    return d.to_bytes()


def cut_opening(data, tree, box, inside=False):
    """Delete faces of `tree` that reach into `box` and aren't floor (faces
    whose normal points mostly up are kept), then rebuild the BSP trees.
    With `inside`, only faces lying entirely within the box go (a doorway cut
    along existing edges, leaving the faces next to it alone)."""
    import geometry
    import analyze
    from edit import _face_normal
    x0, y0, z0, x1, y1, z1 = box
    te = geometry.TopologyEdit(data).from_level()
    pos = analyze.material_positions(te.L)
    kept, removed = [], 0
    for entry in te.tree_faces[tree]:
        tri = entry[0]
        pts = [pos[v] for v in tri]
        lo = [min(p[k] for p in pts) for k in range(3)]
        hi = [max(p[k] for p in pts) for k in range(3)]
        if inside:
            hit = lo[0] >= x0 and hi[0] <= x1 and lo[1] >= y0 and hi[1] <= y1 and lo[2] >= z0 and hi[2] <= z1
        else:
            hit = lo[0] <= x1 and hi[0] >= x0 and lo[1] <= y1 and hi[1] >= y0 and lo[2] <= z1 and hi[2] >= z0
        fake = type("F", (), {"v0": tri[0], "v1": tri[1], "v2": tri[2]})
        n = _face_normal(pos, fake)
        floor = n is not None and n[2] > 0.7
        if hit and not floor:
            removed += 1
        else:
            kept.append(entry)
    te.tree_faces[tree] = kept
    out, _ = te.build()
    return out, "removed %d faces of tree %d inside %s" % (removed, tree, box)


def check_new_name(bf, name):
    nid = disc.hash_unit(name)
    if any(sd[0] == nid for sd in bf.subdirs):
        raise SystemExit("unit %s collides with an existing bigfile directory (ID %d)" % (name, nid))
    if not name[:1].isalpha() or not any(ch.isdigit() for ch in name):
        raise SystemExit("unit names are an area name followed by a number, e.g. under5")
    return nid


def unit_path(unit, ext, mods=None):
    p = disc.level_path(unit).replace(".drm", "." + ext)
    return p if mods is None else os.path.join(mods, *p.strip("\\").split("\\"))


def _read_input(bf, unit, mods, drm_path=None):
    """The unit's level: --drm file, else the mods folder's copy, else the disc."""
    if drm_path:
        with open(drm_path, "rb") as f:
            return f.read(), drm_path
    if mods:
        p = unit_path(unit, "drm", mods)
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read(), p
    return bf.read_level(unit), "disc"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--disc", required=True, help="CD image containing BIGFILE.DAT")
    ap.add_argument("--install", metavar="MODS_DIR", help="the port's mods folder (read and written)")
    ap.add_argument("-o", "--output-dir", help="write the files here instead of --install")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="make a new unit as a copy of an existing one")
    n.add_argument("source")
    n.add_argument("name")
    n.add_argument("--tint", nargs=3, type=int, metavar=("R", "G", "B"), help="tint its vertex colours")
    n.add_argument("--disable-portal", action="append", default=[], metavar="UNIT",
                   help="remove the copy's portal to UNIT (repeatable)")
    n.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270),
                   help="turn the copy about the vertical axis (degrees, counter-clockwise)")
    r = sub.add_parser("retarget", help="make a unit's portals to OLD lead to NEW")
    r.add_argument("unit")
    r.add_argument("old")
    r.add_argument("new")
    r.add_argument("--drm", help="the unit's level file (default: mods copy, else the disc)")
    r.add_argument("--number", type=int, help="also set the portal number on the other side")
    mp = sub.add_parser("move-portal", help="move a unit's portal (record and trigger faces)")
    mp.add_argument("unit")
    mp.add_argument("index", type=int, help="portal index, as leveldump lists them")
    mp.add_argument("delta", nargs=3, type=int, metavar=("DX", "DY", "DZ"))
    mp.add_argument("--drm")
    ad = sub.add_parser("add-portal", help="add a portal to DEST, arriving at DEST's portal DEST_NUM")
    ad.add_argument("unit")
    ad.add_argument("dest")
    ad.add_argument("dest_num", type=int)
    ad.add_argument("--like", metavar="UNIT:INDEX", help="copy the shape of another unit's portal")
    ad.add_argument("--at", nargs=3, type=int, metavar=("X", "Y", "Z"), help="put the copied shape's min corner here")
    ad.add_argument("--quad", nargs=12, type=int, metavar="C", help="corners A B C D (x y z each): t1 = ABC, t2 = ACD")
    ad.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270),
                    help="turn the --like shape first (degrees, counter-clockwise)")
    rp = sub.add_parser("repair-signals", help="fix signals emptied by an older 'disable'")
    rp.add_argument("unit")
    rp.add_argument("--drm")
    co = sub.add_parser("cut-opening", help="delete the walls and ceiling of a tree inside a box")
    co.add_argument("unit")
    co.add_argument("box", nargs=6, type=int, metavar="B", help="x0 y0 z0 x1 y1 z1")
    co.add_argument("--tree", type=int, default=0)
    co.add_argument("--drm")
    ad.add_argument("--drm")
    x = sub.add_parser("disable", help="remove a unit's portal to UNIT")
    x.add_argument("unit")
    x.add_argument("dest")
    x.add_argument("--drm")
    args = ap.parse_args(argv)
    if not (args.install or args.output_dir):
        ap.error("give --install or -o")
    bf = disc.BigFile(args.disc)
    out_root = args.install or args.output_dir

    written = {}       # unit -> level bytes, for the video-memory check

    def write(path, blob):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
        print("wrote", path)
        if path.lower().endswith(".drm"):
            written[os.path.splitext(os.path.basename(path))[0].lower()] = blob

    if args.cmd == "new":
        check_new_name(bf, args.name)
        data, src = _read_input(bf, args.source, args.install)
        print("copying %s (from %s)" % (args.source, src))
        data, notes = rename(data, args.source, args.name)
        for line in notes:
            print(line)
        for dest in args.disable_portal:
            data, msg = disable(data, dest)
            print(msg)
        if args.rotate:
            import transform
            data, tnotes = transform.rotate(data, args.rotate // 90)
            for line in tnotes:
                print(line)
        if args.tint:
            data = tint(data, args.tint)
            print("vertex colours tinted by", tuple(args.tint))
        walk_signals(parse_level(data).drm)        # sanity: the signal list must walk
        write(unit_path(args.name, "drm", out_root), data)
        for ext in ("crm", "snf", "smf"):
            try:
                blob = bf.read_file(unit_path(args.source, ext), args.source)
            except FileNotFoundError:
                continue
            mp = unit_path(args.source, ext, args.install) if args.install else None
            if mp and os.path.exists(mp):
                with open(mp, "rb") as f:
                    blob = f.read()
            write(unit_path(args.name, ext, out_root), blob)
        units_txt = os.path.join(out_root, "units.txt")
        lines = []
        if os.path.exists(units_txt):
            with open(units_txt) as f:
                lines = [ln for ln in f if not ln.split("=")[0].strip().lower() == args.name.lower()]
        lines.append("%s = %s\n" % (args.name, args.source))
        with open(units_txt, "w") as f:
            f.writelines(lines)
        print("wrote", units_txt, "(%s uses %s's bigfile directory)" % (args.name, args.source))
    else:
        data, src = _read_input(bf, args.unit, args.install, args.drm)
        print("editing %s (from %s)" % (args.unit, src))
        import portals
        if args.cmd == "retarget":
            data, msg = retarget(data, args.old, args.new, args.number)
        elif args.cmd == "move-portal":
            data, msg = portals.move(data, args.index, tuple(args.delta))
        elif args.cmd == "add-portal":
            if args.like:
                u, i = args.like.split(":")
                other, _ = _read_input(bf, u, args.install)
                _, _, t1, t2, _ = portals.portal_quads(parse_level(other))[int(i)]
                if args.rotate:
                    import transform
                    t1 = transform.turn_points(t1, args.rotate // 90)
                    t2 = transform.turn_points(t2, args.rotate // 90)
                if args.at:
                    lo = [min(p[k] for p in t1 + t2) for k in range(3)]
                    t1 = [tuple(p[k] - lo[k] + args.at[k] for k in range(3)) for p in t1]
                    t2 = [tuple(p[k] - lo[k] + args.at[k] for k in range(3)) for p in t2]
            elif args.quad:
                c = [tuple(args.quad[i:i + 3]) for i in range(0, 12, 3)]
                t1, t2 = [c[0], c[1], c[2]], [c[0], c[2], c[3]]
            else:
                ap.error("give --like (with --at) or --quad")
            data, num = portals.add(data, args.dest, args.dest_num, t1, t2)
            msg = ("added portal %d -> %s,%d with quad %s %s; %s needs a portal back to %s,%d"
                   % (num, args.dest, args.dest_num, t1, t2, args.dest, args.unit, num))
        elif args.cmd == "repair-signals":
            data, n = repair_signals(data)
            msg = "repaired %d signal(s)" % n
        elif args.cmd == "cut-opening":
            data, msg = cut_opening(data, args.tree, args.box)
        else:
            data, msg = disable(data, args.dest)
        print(msg)
        walk_signals(parse_level(data).drm)        # must walk like the engine walks it
        write(unit_path(args.unit, "drm", out_root), data)
    if written:
        vrambudget.check_files(args.disc, args.install, list(written), written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
