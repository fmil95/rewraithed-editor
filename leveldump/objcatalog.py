r"""Every object type the game places, with a unit to copy it from and its model.

Walks the units reachable through portals (from the disc, or the mods folder
where a unit is edited), collects the intro types, and writes one JSON file:

    {"types": {"hunter": {"units": {"city4": 3, ...}, "count": 12,
                          "template": "city4", "kind": "creature",
                          "vram": [w, h], "deps": ["fthrow"],
                          "model": {"verts": [[x, y, z]...], "faces": [[a, b, c]...]}}, ...},
     "units": ["city9", ...]}

`template` is a unit that lists the type in its object list and has an intro
of it: what levelpatch.py --intro-templates needs to add the type to another
unit. `kind` is start (Raziel), boss, creature, effect (nothing to see) or
object. Model vertices are in game units around the object's origin, in its
first model's rest pose (each segment's vertices plus its pivot and those of
its parents), Z up; models face -Y. Type names are lower case (intros spell
some differently, e.g. "Hunter").

    python objcatalog.py --disc KAIN2.img [--mods <exe folder>/mods] --out catalog.json
"""

import argparse
import json
import os
import sys

from drm import Drm
from vrambudget import Source, neighbours, object_names

SEEDS = ["under1", "city9"]
SKIP = {"marker", "campath"}       # the engine's own markers: they belong to their unit
BOSSES = {"kain", "alukabss", "roninbss", "skinbos", "walboss", "walbosb"}


def model(data):
    """(verts, faces) of the object's first model, or None."""
    d = Drm(data)
    if d.s16(0x08) < 1:
        return None
    ml = d.ptr(0x0C)
    if ml is None:
        return None
    m = d.ptr(ml)
    if m is None:
        return None
    nv, vl = d.s32(m), d.ptr(m + 4)
    nf, fl = d.s32(m + 0x10), d.ptr(m + 0x14)
    ns, sl = d.s32(m + 0x18), d.ptr(m + 0x1C)
    if not nv or vl is None or not 0 < nv < 20000 or not 0 <= nf < 40000:
        return None
    verts = [list(d.unpack("hhh", vl + i * 8)) for i in range(nv)]
    if sl is not None and 0 < ns < 200:
        segs = []
        for i in range(ns):
            o = sl + i * 0x18
            first, last = d.s16(o + 8), d.s16(o + 10)
            px, py, pz = d.unpack("hhh", o + 12)
            segs.append((first, last, (px, py, pz), d.s16(o + 18)))
        absolute = []
        for i, (_, _, p, parent) in enumerate(segs):
            if 0 <= parent < i:
                q = absolute[parent]
                absolute.append((p[0] + q[0], p[1] + q[1], p[2] + q[2]))
            else:
                absolute.append(p)
        for i, (first, last, _, _) in enumerate(segs):
            a = absolute[i]
            for v in range(max(first, 0), min(last + 1, nv)):
                verts[v] = [verts[v][0] + a[0], verts[v][1] + a[1], verts[v][2] + a[2]]
    faces = []
    if fl is not None:
        for i in range(nf):
            a, b, c = d.unpack("HHH", fl + i * 12)
            if max(a, b, c) < nv and len({a, b, c}) == 3:
                faces.append([a, b, c])
    return verts, faces


def kind(typ, data, mdl):
    """What the editor shows it as: 'start' (Raziel), 'boss', 'creature' (monster
    attributes: the MON_ code drives it), 'effect' (no model to see: flames,
    sounds, helpers) or 'object'."""
    if typ == "raziel":
        return "start"
    d = Drm(data)
    attr = d.ptr(0x1C) if d.is_ptr(0x1C) else None
    creature = attr is not None and attr + 4 <= len(d) and d.unpack("I", attr)[0] == 0xACE00065
    if typ in BOSSES:
        return "boss"
    if mdl is None or len(mdl[1]) < 2:
        return "effect"
    return "creature" if creature else "object"


def walk(src, seeds):
    seen, todo, order = set(), [s.lower() for s in seeds], []
    while todo:
        u = todo.pop(0)
        if u in seen:
            continue
        seen.add(u)
        L = src.level(u)
        if L is None:
            continue
        order.append(u)
        todo += [n for n in neighbours(L) if n not in seen]
    return order


def catalog(src, seeds=SEEDS, models=True):
    units = walk(src, seeds)
    types = {}
    for u in units:
        L = src.level(u)
        listed = set(object_names(L))
        for it in L.terrain.intros:
            t = it.name.lower()
            if t in SKIP:
                continue
            e = types.setdefault(t, {"units": {}, "count": 0, "template": None, "_score": -1})
            e["units"][u] = e["units"].get(u, 0) + 1
            e["count"] += 1
            # a unit that loads the type itself, an intro with its data block, few
            # neighbours' worth of instances: the plainest copy
            score = (2 if t in listed else 0) + (1 if it.has_data else 0)
            if score > e["_score"]:
                e["template"], e["_score"] = u, score
    out = {}
    for t in sorted(types):
        e = types[t]
        del e["_score"]
        data = src._object_data(t)
        if data is None:
            continue
        try:
            m = model(data)
        except Exception:          # an odd object file: no model, still placeable
            m = None
        e["kind"] = kind(t, data, m)
        size = src.object_size(t)
        e["vram"] = list(size) if size else None
        e["deps"] = src.object_deps(t)
        e["model"] = {"verts": m[0], "faces": m[1]} if m and models else None
        out[t] = e
    return {"types": out, "units": units}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--disc", required=True)
    ap.add_argument("--mods", metavar="MODS_DIR", help="read edited units and objects from the mods folder")
    ap.add_argument("--out", required=True, help="JSON file to write")
    ap.add_argument("--no-models", action="store_true")
    ap.add_argument("seeds", nargs="*", default=SEEDS, help="units to walk from (default: %s)" % " ".join(SEEDS))
    args = ap.parse_args(argv)
    src = Source(args.disc, args.mods)
    cat = catalog(src, args.seeds or SEEDS, not args.no_models)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(cat, f, separators=(",", ":"))
    kinds = {}
    for e in cat["types"].values():
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("%d object types in %d units (%s) -> %s" % (
        len(cat["types"]), len(cat["units"]),
        ", ".join("%d %s" % (n, k) for k, n in sorted(kinds.items())), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
