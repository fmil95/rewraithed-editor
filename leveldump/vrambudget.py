r"""Object video-memory budget: will every object type near a unit get its textures?

Each object type (not each instance) gets one block of video memory for its
textures, w x h halfwords from its Object.vramSize. STREAM_PackVRAMObject asks
VRAM_GetObjectVramSpace for that block in the object area, the lower right
quarter of video memory (x 512..1023, y 256..511), which the game shares with
the font (16 x 128) and the objects the engine keeps loaded everywhere
(Raziel, the glyph icons, particles, the soul and health effects). If the block does not fit, the game compacts the area
(VRAM_RearrangeVramsLayer) and tries again; if it still does not fit the
object is used anyway without a block, and its textures point at whatever is
in video memory: they look scrambled.

The game keeps an object type loaded while any loaded unit lists it, and the
loaded units are the current one and the units its portals lead to. So the
object types that must fit together are those of a unit and its neighbours:
its *group*. An edit to one unit changes the group of every neighbour too.

This module simulates the game's allocator (a port of VRAM.c: the free list
kept in area order, the 64-halfword texture page rule and the block merging)
for each group, placing the blocks the way the compaction does (largest
first), which is the best the game can do. Objects still resident from units
you passed through, or spawned at run time, are not modelled; the port's
-vramlog flag logs the real placements to calibrate against.

    python vrambudget.py --disc KAIN2.img --install <exe folder>/mods city9
    python vrambudget.py --disc KAIN2.img --survey under1 city9   # the original game, from these units outwards
"""

import argparse
import os
import re
import struct
import sys

import disc
from drm import Drm
from level import parse_level

AREA = (512, 256, 512, 256)       # x, y, w, h of the object area
FONT = ("(font)", 16, 128)
# loaded by the engine in every unit, not listed in objectNameList (seen with -vramlog)
ALWAYS = ["raziel", "glphicon", "soul", "force", "particle", "healths", "eaggot", "eaggots"]
_NAME = re.compile(rb"(?<![\x21-\x7e])([a-zA-Z][a-zA-Z0-9_]{2,7})\x00")


# --- the game's allocator (VRAM.c) ------------------------------------------

class _Block:
    __slots__ = ("x", "y", "w", "h", "name")

    def __init__(self, x, y, w, h, name=None):
        self.x, self.y, self.w, self.h, self.name = x, y, w, h, name

    @property
    def area(self):
        return self.w * self.h


class Allocator:
    def __init__(self, area=AREA):
        self.free = []            # kept in ascending area order, like openVramBlocks
        self.used = []
        self.insert_free_vram(*area)

    def _insert_free_block(self, b):
        i = 0
        while i < len(self.free) and self.free[i].area < b.area:
            i += 1
        self.free.insert(i, b)
        while self._concatenate():
            pass

    def _concatenate(self):
        f = self.free
        for i, cur in enumerate(f):
            for nxt in f[i + 1:]:
                if cur.x == nxt.x and cur.w == nxt.w and (cur.y >> 8) == (nxt.y >> 8):
                    if cur.y + cur.h == nxt.y:
                        cur.h += nxt.h
                        f.remove(nxt)
                        return True
                    if nxt.y + nxt.h == cur.y:
                        nxt.h += cur.h
                        f.remove(cur)
                        return True
                if cur.y == nxt.y and cur.h == nxt.h:
                    if cur.x + cur.w == nxt.x and (not cur.x & 0x3F or cur.w + nxt.w < 65):
                        cur.w += nxt.w
                        f.remove(nxt)
                        return True
                    if nxt.x + nxt.w == cur.x and (not nxt.x & 0x3F or cur.w + nxt.w < 65):
                        nxt.w += cur.w
                        f.remove(cur)
                        return True
        return False

    def insert_free_vram(self, x, y, w, h):
        if x & 0x3F and w > 64 - (x & 0x3F):
            self._insert_free_block(_Block(x, y, 64 - (x & 0x3F), h))
            self._insert_free_block(_Block(x + 64 - (x & 0x3F), y, w - 64 + (x & 0x3F), h))
        else:
            self._insert_free_block(_Block(x, y, w, h))

    def check_slot(self, w, h, name, start_y=256):
        """VRAM_CheckVramSlot: the placed block, or None."""
        chosen, fits, right, right_off = None, False, None, 0
        for b in list(self.free):
            if not (w <= b.w and h <= b.h and (start_y == -1 or start_y <= b.y < start_y + 256)):
                continue
            newx = 0x40 - (b.x & 0x3F)
            if not b.x & 0x3F:
                chosen = b
                break
            if newx >= w:
                if not b.x & 0xF:
                    chosen = b
                    break
                off = 0x10 - (b.x & 0xF)
                if 0x40 - ((b.x + off) & 0x3F) >= w and b.w - off >= w:
                    self.insert_free_vram(b.x, b.y, off, b.h)
                    b.x += off
                    b.w -= off
                    chosen = b
                    break
            else:
                off = newx
                if 0x40 - ((b.x + off) & 0x3F) >= w and b.w - off >= w:
                    fits, right, right_off = True, b, off
        if chosen is None and fits:
            b = right
            self.insert_free_vram(b.x, b.y, right_off, b.h)
            b.x += right_off
            b.w -= right_off
            chosen = b
        if chosen is None:
            return None
        b = chosen
        hx, hy, hw, hh = b.x, b.y, b.w, b.h
        if b in self.free:
            self.free.remove(b)
        used = _Block(hx, hy, w, h, name)
        self.used.insert(0, used)
        if hw == w:
            if hh != h:
                self.insert_free_vram(hx, hy + h, w, hh - h)
        elif hh == h:
            self.insert_free_vram(hx + w, hy, hw - w, hh)
        elif abs((hw - w) * h - hw * (hh - h)) > abs((hw - w) * hh - w * (hh - h)):
            self.insert_free_vram(hx + w, hy, hw - w, h)
            self.insert_free_vram(hx, hy + h, hw, hh - h)
        else:
            self.insert_free_vram(hx + w, hy, hw - w, hh)
            self.insert_free_vram(hx, hy + h, w, hh - h)
        return used


def pack(blocks):
    """Place [(name, w, h)] the way VRAM_RearrangeVramsLayer does (largest area
    first). Returns (placed [(name, x, y, w, h)], failed [(name, w, h)])."""
    a = Allocator()
    order = sorted(blocks, key=lambda b: -b[1] * b[2])     # stable: equal areas keep their order
    placed, failed = [], []
    for name, w, h in order:
        start_y = -1 if name == FONT[0] else 256
        b = a.check_slot(w, h, name, start_y)
        if b is None:
            failed.append((name, w, h))
        else:
            placed.append((name, b.x, b.y, w, h))
    return placed, failed


def load_in_order(blocks):
    """Allocate [(name, w, h)] one by one the way objects stream in: each
    block where it fits, and when it doesn't, compact everything placed so far
    (largest first) and try once more. Returns the names left without a block,
    which are the ones drawn with scrambled textures."""
    have, failed = [], []
    a = Allocator()
    for name, w, h in blocks:
        start_y = -1 if name == FONT[0] else 256
        if a.check_slot(w, h, name, start_y) is not None:
            have.append((name, w, h))
            continue
        a = Allocator()
        for n2, w2, h2 in sorted(have, key=lambda b: -b[1] * b[2]):
            a.check_slot(w2, h2, n2, -1 if n2 == FONT[0] else 256)
        if a.check_slot(w, h, name, start_y) is not None:
            have.append((name, w, h))
        else:
            failed.append((name, w, h))
    return failed


def capacity_left(blocks, w=16, h=256):
    """How many more w x h blocks (a small object) would still fit."""
    n = 0
    while n < 64:
        _, failed = pack(blocks + [("(probe%d)" % i, w, h) for i in range(n + 1)])
        if failed:
            break
        n += 1
    return n


# --- reading units and objects ----------------------------------------------

class Source:
    """Units and object files, from the mods folder when there and else the disc."""

    def __init__(self, disc_path, mods=None, overrides=None):
        self.bf = disc.BigFile(disc_path)
        self.mods = mods
        self.overrides = {k.lower(): v for k, v in (overrides or {}).items()}   # unit -> level bytes
        self._levels = {}
        self._sizes = {}
        self._deps = {}
        self._exists = {}

    def _mod(self, path):
        if self.mods:
            p = os.path.join(self.mods, *path.strip("\\").split("\\"))
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return f.read()
        return disc.read_layers(path)

    def level(self, unit):
        unit = unit.lower()
        if unit in self.overrides:
            return parse_level(self.overrides[unit])
        if unit not in self._levels:
            data = self._mod(disc.level_path(unit))
            if data is None:
                try:
                    data = self.bf.read_level(unit)
                except FileNotFoundError:
                    data = None
            self._levels[unit] = parse_level(data) if data else None
        return self._levels[unit]

    def _object_data(self, typ):
        path = r"\kain2\object\%s\%s.drm" % (typ, typ)
        data = self._mod(path)
        if data is None:
            try:
                data = self.bf.read_file(path)
            except FileNotFoundError:
                data = None
        return data

    def is_object(self, typ):
        if typ not in self._exists:
            path = r"\kain2\object\%s\%s.drm" % (typ, typ)
            ok = self._mod(path) is not None
            if not ok:
                try:
                    self.bf.lookup(path)
                    ok = True
                except FileNotFoundError:
                    pass
            self._exists[typ] = ok
        return self._exists[typ]

    def object_deps(self, typ):
        """Object types a type loads itself (a hunter's crossbow and fire
        bolt): the names in its data that are object files too."""
        typ = typ.lower()
        if typ not in self._deps:
            out = []
            data = self._object_data(typ)
            if data:
                for m in _NAME.finditer(bytes(Drm(data).body)):
                    n = m.group(1).decode("latin-1").lower()
                    if n != typ and n not in out and self.is_object(n):
                        out.append(n)
            self._deps[typ] = out
        return self._deps[typ]

    def object_size(self, typ):
        """(w, h) of the type's video-memory block, or None if it needs none."""
        typ = typ.lower()
        if typ not in self._sizes:
            data = self._object_data(typ)
            size = None
            if data:
                x, y, w, h = struct.unpack_from("<4h", Drm(data).body, 0x44)
                if x != -1 and w > 0 and h > 0:
                    size = (w, h)
            self._sizes[typ] = size
        return self._sizes[typ]


def object_names(L):
    d = L.drm
    o = d.ptr(0x8C)
    names = []
    if o is None:
        return names
    while d.body[o] != 0xFF:
        names.append(d.cstr(o, 16).lower())
        o += 16
    return names


def neighbours(L):
    out = []
    for p in L.terrain.portals:
        n = p.dest_unit.lower()
        if n and n not in out:
            out.append(n)
    return out


def group(src, unit):
    """The units loaded while standing in `unit`, and the object types they need:
    ([units], {type: [units listing it]})."""
    unit = unit.lower()
    L = src.level(unit)
    units = [unit] + [n for n in neighbours(L) if src.level(n) is not None]
    types = {t: ["(engine)"] for t in ALWAYS}
    for u in units:
        for t in object_names(src.level(u)):
            types.setdefault(t, []).append(u)
    todo = list(types)
    while todo:
        t = todo.pop()
        for dep in src.object_deps(t):
            if dep not in types:
                types[dep] = []
                todo.append(dep)
            via = "(via %s)" % t
            if via not in types[dep]:
                types[dep].append(via)
    return units, types


def blocks_for(src, types):
    blocks = [FONT]
    for t in types:
        s = src.object_size(t)
        if s:
            blocks.append((t, s[0], s[1]))
    return blocks


def report_group(src, unit):
    units, types = group(src, unit)
    blocks = blocks_for(src, types)
    placed, failed = pack(blocks)
    if failed:
        # which ones lose out: the font and the engine's objects are there
        # first, then the unit's own types, then its neighbours'
        failed = load_in_order(blocks) or failed
    width = sum(w * h for _, w, h in blocks) / AREA[3]
    return dict(unit=unit, units=units, types=types, blocks=blocks, placed=placed,
                failed=failed, width=width, spare=0 if failed else capacity_left(blocks))


def format_report(r, verbose=False):
    lines = []
    head = "%-8s %3.0f%% of the object area (%d of %d columns)" % (
        r["unit"], 100.0 * r["width"] / AREA[2], round(r["width"]), AREA[2])
    if r["failed"]:
        head += ": OVER, no room for " + ", ".join("%s (%dx%d)" % f for f in r["failed"])
    else:
        head += ", room for %d more 16-wide object%s" % (r["spare"], "" if r["spare"] == 1 else "s")
    lines.append(head)
    if verbose or r["failed"]:
        lines.append("         units: " + ", ".join(r["units"]))
        sizes = {n: (w, h) for n, w, h in r["blocks"]}
        for t, us in sorted(r["types"].items()):
            s = sizes.get(t)
            lines.append("         %-8s %-8s from %s" % (t, "%dx%d" % s if s else "none", ", ".join(us)))
    return lines


def affected_units(src, edited):
    """The edited units and every unit whose group contains one of them."""
    out = []
    for u in edited:
        u = u.lower()
        L = src.level(u)
        if L is None:
            continue
        for v in [u] + neighbours(L):
            if v not in out and src.level(v) is not None:
                out.append(v)
    return out


def check(src, edited, label=""):
    """Print the budget of every group an edit touches; returns the number over."""
    over = 0
    print("object video memory%s:" % (" (" + label + ")" if label else ""))
    for u in affected_units(src, edited):
        r = report_group(src, u)
        for line in format_report(r):
            print("  " + line)
        if r["failed"]:
            over += 1
    if over:
        print("  warning: %d group%s over budget; those objects will have scrambled textures"
              % (over, "" if over == 1 else "s"))
    return over


def check_files(disc_path, mods, edited, overrides=None, label=""):
    """check() for tools that only have file paths (levelpatch, units, newunit).
    `overrides` maps units to level bytes not yet written."""
    if not disc_path:
        print("object video memory: not checked (needs --disc to read the object files)")
        return 0
    try:
        return check(Source(disc_path, mods, overrides), edited, label)
    except Exception as e:      # a budget problem must never stop an edit from being written
        print("object video memory: not checked (%s)" % e)
        return 0


def survey(src, seeds, limit=2000):
    """Every unit reachable through portals from `seeds`, with its group's budget."""
    seen, todo, rows = set(), [s.lower() for s in seeds], []
    while todo and len(seen) < limit:
        u = todo.pop(0)
        if u in seen:
            continue
        seen.add(u)
        L = src.level(u)
        if L is None:
            continue
        r = report_group(src, u)
        rows.append(r)
        todo += [n for n in neighbours(L) if n not in seen]
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--disc", required=True)
    ap.add_argument("--install", metavar="MODS_DIR", help="read edited units and objects from the mods folder")
    ap.add_argument("--layer", metavar="DIR", action="append", default=[],
                    help="also read files from DIR, after --install (a prototype's maps: proto/<date>/areas)")
    ap.add_argument("--survey", action="store_true",
                    help="walk every unit reachable from the given ones and rank their groups")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("units", nargs="+")
    args = ap.parse_args(argv)
    disc.LAYERS[:] = args.layer
    src = Source(args.disc, args.install)
    if args.survey:
        rows = survey(src, args.units)
        rows.sort(key=lambda r: (-len(r["failed"]), -r["width"]))
        print("%d units; largest groups:" % len(rows))
        for r in rows[:args.top]:
            for line in format_report(r, args.verbose):
                print("  " + line)
        return 0
    over = 0
    for u in affected_units(src, args.units) if not args.verbose else args.units:
        r = report_group(src, u)
        for line in format_report(r, args.verbose):
            print(line)
        over += bool(r["failed"])
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
