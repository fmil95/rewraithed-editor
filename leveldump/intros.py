"""Intros (object and marker placements) as Blender markers, and back.

Export: `<unit>_intros.obj`, one small arrow per intro, named
"intro_<UniqueID>_<type>". The arrow's first vertex is the intro's position and
the second the tip, pointing along the intro's Z rotation (yaw), so moving or
rotating the object in Blender (Object Mode is fine: the OBJ exporter applies
transforms) moves or turns the intro. Arrows are drawn at the material
position; `spectralPosition` (an offset used in the spectral realm) is kept.

Import (`apply_intros`), on the full intros OBJ:
* an existing intro whose arrow moved gets the new position; its spline's
  positional keys move with it (as STREAM_AdjustMultiSpline does when a unit
  is relocated); a turned arrow sets rotation z (4096 = one turn), x and y
  rotation are kept;
* an intro whose arrow is gone is deleted;
* a duplicated arrow ("....001" in Blender) becomes a new intro copied from
  the original: same data block (the engine already shares intro data between
  instances), no spline, a fresh UniqueID;
* an arrow named after an UniqueID or type the level doesn't have, e.g.
  "intro_new_urnx", becomes a new intro of that type, copied from an intro of
  that type in this level, else from one in the template units; a type the
  unit doesn't load yet is appended to Level.objectNameList. A template from
  another unit brings a copy of its data block (copy_intro_data), with the
  commands that name things of the source unit adjusted.

Nothing else in a level points into the intro list (checked on all units:
only Level.introList and Terrain.introList, which are the same pointer, and
the two copies of the count). Scripts refer to intros by UniqueID, so
existing UniqueIDs never change; deleting an intro a script uses can still
break that script.
"""

import math
import re
import struct

from drm import Drm
from level import SIZEOF_INTRO, parse_level

ARROW = 60          # game units
SPECIAL_TYPES = {"raziel", "marker", "campath"}   # placed by the engine, not in objectNameList
NAME = re.compile(r"^intro_([^_]+)_(.+?)(\.\d+)?$")


def _yaw_dir(rot_z):
    a = rot_z * 2 * math.pi / 4096.0
    return (-math.sin(a), math.cos(a))     # rotation 0 points along +Y


def write_intros_obj(path, level, scale=0.01):
    t = level.terrain
    with open(path, "w", newline="\n") as f:
        f.write("# %s intros, exported by rewraithed-editor leveldump\n" % level.worldName)
        f.write("# first vertex = position, second = facing (rotation z); scale %g, Z up\n" % scale)
        n = 1
        for it in t.intros:
            x, y, z = it.position
            dx, dy = _yaw_dir(it.rotation[2])
            px, py = -dy, dx
            pts = [(x, y, z), (x + dx * ARROW, y + dy * ARROW, z),
                   (x + px * ARROW * 0.35, y + py * ARROW * 0.35, z),
                   (x - px * ARROW * 0.35, y - py * ARROW * 0.35, z),
                   (x, y, z + ARROW)]
            f.write("o intro_%d_%s\n" % (it.UniqueID, it.name))
            for p in pts:
                f.write("v %.4f %.4f %.4f\n" % (p[0] * scale, p[1] * scale, p[2] * scale))
            for a, b, c in ((0, 1, 2), (0, 3, 1), (2, 1, 4), (1, 3, 4), (3, 2, 4)):
                f.write("f %d %d %d\n" % (n + a, n + b, n + c))
            n += 5
    return path


def _read_arrows(path, scale):
    arrows = []           # (object name, [(x, y, z) game units])
    cur = None
    with open(path) as f:
        for line in f:
            if line.startswith("o "):
                cur = [line[2:].strip(), []]
                arrows.append(cur)
            elif line.startswith("v ") and cur is not None:
                p = [float(v) for v in line.split()[1:4]]
                cur[1].append(tuple(p[k] / scale for k in range(3)))
    return [(n, v) for n, v in arrows if n.startswith("intro_")]


def _object_names(d):
    o = d.ptr(0x8C)
    names = []
    if o is None:
        return names, None
    while d.body[o] != 0xFF:
        names.append(d.cstr(o, 16))
        o += 16
    return names, o


class IntroTemplate:
    def __init__(self, record, has_data_ptr, data_ptr, foreign):
        self.record = bytearray(record)
        self.data_ptr = data_ptr          # body offset in this level, or None
        self.foreign = foreign


def apply_intros(data, obj_path, scale=0.01, template_levels=()):
    r"""Return (new level bytes, report lines, {new object type: unit it came from}).

    A new object type's files (\kain2\object\<type>\<type>.drm etc.) are only in
    the bigfile directories of the units that use it, so the caller has to
    provide them (levelpatch.py --install copies them into the mods folder)."""
    L = parse_level(data)
    d = Drm(data)
    t = L.terrain
    report = []
    by_uid = {it.UniqueID: it for it in t.intros}
    arrows = _read_arrows(obj_path, scale)
    if not arrows:
        raise SystemExit("%s has no intro_ objects" % obj_path)

    il = t.ptrs["introList"]
    old_n = t.numIntros
    records = []          # [bytearray(0x4C), {field offset: pointer value or None}]
    seen = set()
    next_uid = max([it.UniqueID for it in t.intros] + [0]) + 1
    names_loaded, _ = _object_names(d)
    add_types = []
    add_sources = {}
    moved = turned = added = 0

    def ptr_fields(it):
        return {k: (d.ptr(it.offset + k) if d.is_ptr(it.offset + k) else None) for k in (0x30, 0x34, 0x38, 0x3C)}

    templates = {}
    for tl in template_levels:
        for it in tl.terrain.intros:
            templates.setdefault(it.name, (tl, it))

    for name, verts in arrows:
        m = NAME.match(name)
        if not m or len(verts) < 2:
            report.append("ignored object %r (not an intro arrow)" % name)
            continue
        uid_s, typ, dup = m.group(1), m.group(2), m.group(3)
        pos = tuple(int(round(c)) for c in verts[0])
        dx, dy = verts[1][0] - verts[0][0], verts[1][1] - verts[0][1]
        yaw = int(round(math.atan2(-dx, dy) * 4096 / (2 * math.pi))) & 0xFFF
        uid = int(uid_s) if uid_s.isdigit() else None
        src = by_uid.get(uid) if uid is not None else None

        if src is not None and uid not in seen and not dup:
            seen.add(uid)
            rec = bytearray(d.body[src.offset: src.offset + SIZEOF_INTRO])
            ptrs = ptr_fields(src)
            if pos != src.position:
                struct.pack_into("<hhh", rec, 0x20, *pos)
                delta = tuple(pos[k] - src.position[k] for k in range(3))
                _shift_spline(d, ptrs[0x38], delta)
                moved += 1
            old_yaw = src.rotation[2] & 0xFFF
            if min((yaw - old_yaw) & 0xFFF, (old_yaw - yaw) & 0xFFF) > 8:   # ignore round-off
                struct.pack_into("<h", rec, 0x1C, yaw - 4096 if yaw >= 2048 else yaw)
                turned += 1
            records.append([rec, ptrs])
            continue

        # a new intro: from the duplicated original, else same type here, else a template unit
        tmpl_it = src or next((it for it in t.intros if it.name == typ), None)
        if tmpl_it is not None:
            rec = bytearray(d.body[tmpl_it.offset: tmpl_it.offset + SIZEOF_INTRO])
            ptrs = ptr_fields(tmpl_it)
            ptrs[0x38] = None      # no spline: it would need its own copy
            typ = tmpl_it.name
        elif typ in templates:
            tl, tit = templates[typ]
            rec = bytearray(tl.drm.body[tit.offset: tit.offset + SIZEOF_INTRO])
            ptrs = {0x30: None, 0x34: None, 0x38: None, 0x3C: None}
            if tit.has_data:
                block, dropped = copy_intro_data(tl.drm, tl.drm.ptr(tit.offset + 0x30), L.streamUnitID)
                # appended at the end of the body; the intro-list replacement
                # below moves it (and this pointer) along with everything behind
                d.body += b"\0" * (-len(d.body) % 4)
                ptrs[0x30] = len(d.body)
                d.body += block
                report.append("new %s copied from %s with its data block (%d bytes%s)" % (
                    typ, tl.worldName, len(block), "; dropped " + ", ".join(dropped) if dropped else ""))
            tl_names, _ = _object_names(tl.drm)
            if typ not in names_loaded and typ not in add_types and typ in tl_names:
                add_types.append(typ)
                add_sources[typ] = tl.worldName
        else:
            raise SystemExit("no intro of type %r in this level or the template units (--intro-templates)" % typ)
        struct.pack_into("<i", rec, 0x14, next_uid)
        same = [it.intronum for it in t.intros if it.name == typ]
        struct.pack_into("<i", rec, 0x10, max(same + [0]) + 1 if same else 0)
        struct.pack_into("<hhh", rec, 0x20, *pos)
        struct.pack_into("<h", rec, 0x1C, yaw - 4096 if yaw >= 2048 else yaw)
        struct.pack_into("<hhh", rec, 0x44, 0, 0, 0)
        report.append("added %s (UniqueID %d) at %s" % (typ, next_uid, pos))
        next_uid += 1
        records.append([rec, ptrs])
        added += 1

    deleted = [it for it in t.intros if it.UniqueID not in seen]
    for it in deleted:
        report.append("deleted %s (UniqueID %d)" % (it.name, it.UniqueID))

    # --- write: the intro list, the counts, the object name list ----------
    blob = bytearray()
    ptr_locs = []
    for rec, ptrs in records:
        o = len(blob)
        for k in (0x30, 0x34, 0x38, 0x3C):
            struct.pack_into("<I", rec, k, 0)
            if ptrs[k] is not None:
                ptr_locs.append((o + k, ptrs[k]))
        blob += rec
    end = il + SIZEOF_INTRO * old_n
    delta = d.replace(il, end, bytes(blob), [o for o, _ in ptr_locs])
    for o, v in ptr_locs:
        d.pack("I", il + o, v + delta if v >= end else v)
    d.pack("i", 0x78, len(records))
    d.pack("i", d.ptr(0x00) + 0x08, len(records))

    for typ in add_types:
        _insert_object_name(d, typ)
        report.append("%s added to the unit's object list" % typ)

    report.insert(0, "intros: %d -> %d (%d moved, %d turned, %d added, %d deleted)" % (
        old_n, len(records), moved, turned, added, len(deleted)))
    return d.to_bytes(), report, add_sources


# Intro data blocks are INICommand lists: {short command, short numParameters,
# long parameter[numParameters]}..., ending with command 0 (INSTANCE.c,
# MONLIB.c MON_ProcessIntro). All 724 in the game parse that way and none holds a
# pointer. These monster commands name things of the unit the block comes from:
UNIT_SPECIFIC = {
    10: "patrol markers",      # planning-marker IDs, looked up in the monster's own unit
    12: "ambush marker",
    13: "flee marker",
    15: "soul link",           # UniqueID of the soul intro; 0 lets the game create one
}
VALID_UNITS = 14               # stream units the monster may be in


def copy_intro_data(src, off, dest_unit_id):
    """Copy of the command list at body offset `off` of `src` for another unit:
    unit-specific commands dropped, the valid-units list set to the new unit.
    Returns (bytes, [names of dropped commands])."""
    out = bytearray()
    dropped = []
    p = off
    while True:
        cmd, n = src.unpack("hh", p)
        params = list(src.unpack("%di" % n, p + 4)) if n > 0 else []
        p += 4 + 4 * max(n, 0)
        if cmd == 0:
            break
        if cmd in UNIT_SPECIFIC:
            dropped.append(UNIT_SPECIFIC[cmd])
            continue
        if cmd == VALID_UNITS:
            params = [dest_unit_id]
        out += struct.pack("<hh", cmd, len(params)) + struct.pack("<%di" % len(params), *params)
    out += struct.pack("<hh", 0, 0)
    return bytes(out), dropped


def add_object_type(data, typ):
    """Append an object type to Level.objectNameList (if missing). Returns
    (new bytes, whether it was added)."""
    d = Drm(data)
    names, term = _object_names(d)
    if typ in names:
        return data, False
    _insert_object_name(d, typ)
    return d.to_bytes(), True


def _insert_object_name(d, typ):
    """Insert a 16-byte name in front of the list's 0xFF end byte. In an empty
    list that byte is where Level.objectNameList points, and an insertion moves
    every pointer at or behind the insertion point, so the list pointer is put
    back to the start afterwards (else it would skip the new name)."""
    start = d.ptr(0x8C)
    _, term = _object_names(d)
    d.insert(term, 16)
    d.pack("I", 0x8C, start)
    name = typ.encode("latin-1")[:15]
    d.body[term: term + 16] = name + b"\0" * (16 - len(name))


def arrow_obj(name, pos, rot_z, scale=0.01, first=1):
    """OBJ lines for one intro arrow (as write_intros_obj draws them)."""
    x, y, z = pos
    dx, dy = _yaw_dir(rot_z)
    px, py = -dy, dx
    pts = [(x, y, z), (x + dx * ARROW, y + dy * ARROW, z),
           (x + px * ARROW * 0.35, y + py * ARROW * 0.35, z),
           (x - px * ARROW * 0.35, y - py * ARROW * 0.35, z), (x, y, z + ARROW)]
    out = ["o %s" % name] + ["v %.4f %.4f %.4f" % (p[0] * scale, p[1] * scale, p[2] * scale) for p in pts]
    out += ["f %d %d %d" % (first + a, first + b, first + c)
            for a, b, c in ((0, 1, 2), (0, 3, 1), (2, 1, 4), (1, 3, 4), (3, 2, 4))]
    return out


def _shift_spline(d, multi, delta):
    if multi is None or delta == (0, 0, 0):
        return
    spl = d.ptr(multi) if d.is_ptr(multi) else None
    if spl is None:
        return
    keys = d.ptr(spl) if d.is_ptr(spl) else None
    n = d.s16(spl + 4)
    if keys is None:
        return
    for i in range(n):
        o = keys + i * 0x20 + 2
        x, y, z = d.unpack("hhh", o)
        d.pack("hhh", o, x + delta[0], y + delta[1], z + delta[2])
