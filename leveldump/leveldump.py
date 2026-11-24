#!/usr/bin/env python3
"""Dump a Soul Reaver (prototype) level .drm: terrain, portals, intros, morph data, BSP trees.

Examples:
    python leveldump.py --disc path/to/KAIN2.img under1
    python leveldump.py --disc path/to/KAIN2.img under1 city9 --obj out/
    python leveldump.py --drm under1.drm --nodes --json out/under1.json
    python leveldump.py --disc path/to/KAIN2.img under1 --extract out/
"""

import argparse
import collections
import json
import os
import statistics
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze  # noqa: E402
import disc  # noqa: E402
import level as lvl  # noqa: E402
import objexport  # noqa: E402


def _hist(values, buckets):
    out = []
    lo = None
    for b in buckets:
        n = sum(1 for v in values if (lo is None or v > lo) and v <= b)
        out.append(("<=%d" % b if lo is None else "%d-%d" % (lo + 1, b), n))
        lo = b
    out.append((">%d" % buckets[-1], sum(1 for v in values if v > buckets[-1])))
    return "  ".join("%s:%d" % (k, n) for k, n in out if n)


def _fmt_sph(s):
    return "(%d,%d,%d r=%d)" % (s.x, s.y, s.z, s.radius)


def _fmt_box(b):
    return "[%d,%d,%d .. %d,%d,%d]" % (b.minx, b.miny, b.minz, b.maxx, b.maxy, b.maxz)


def report(L, name, args, out=sys.stdout):
    p = lambda *a: print(*a, file=out)  # noqa: E731
    t = L.terrain
    d = L.drm
    p("=" * 78)
    p("%s  (drm body %d bytes, %d relocations, table %d bytes)" % (
        name, len(d), d.num_pointers, d.header_size))
    p("Level: worldName=%r streamUnitID=%d version=0x%08x flags=0x%x unitFlags=0x%x" % (
        L.worldName, L.streamUnitID, L.versionNumber & 0xFFFFFFFF, L.flags, L.unitFlags))
    p("       waterZLevel=%d deathZ=%d bspPlaneError=%d numBSPLeaves=%d" % (
        L.waterZLevel, L.deathZ, L.bspPlaneError, L.numBSPLeaves))
    p("       fog " + " ".join("%s=%d" % kv for kv in L.fog.items()))

    p("")
    p("Terrain @0x%x: UnitChangeFlags=0x%x" % (t.offset, t.UnitChangeFlags & 0xFFFF))
    p("  vertices=%d faces=%d normals=%d intros=%d bspTrees=%d" % (
        t.numVertices, t.numFaces, t.numNormals, t.numIntros, t.numBSPTrees))
    p("  pointers: " + " ".join("%s=%s" % (k, "0x%x" % v if v is not None else "NULL")
                                for k, v in t.ptrs.items()))
    tex = [f for f in t.faces if f.textoff != 0xFFFF]
    p("  faces: %d textured, %d untextured (textoff 0xFFFF)" % (len(tex), t.numFaces - len(tex)))
    attrs = collections.Counter(f.attr for f in t.faces)
    p("  face attr histogram: " + " ".join("0x%02x:%d" % kv for kv in sorted(attrs.items())))
    if t.vertices:
        xs = [v.x for v in t.vertices]; ys = [v.y for v in t.vertices]; zs = [v.z for v in t.vertices]
        p("  vertex extent (local): x %d..%d  y %d..%d  z %d..%d" % (
            min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))

    p("")
    p("StreamUnits: %d portal(s)" % len(t.portals))
    for i, pt in enumerate(t.portals):
        p("  [%d] -> %-12s signal=%s  MSignalID=%d streamID=%d flags=0x%x" % (
            i, pt.dest_unit, pt.dest_signal, pt.MSignalID, pt.streamID, pt.flags & 0xFFFF))
        p("      box (%d,%d,%d)..(%d,%d,%d)  t1=%s t2=%s" % (pt.min + pt.max + (pt.t1, pt.t2)))

    p("")
    names = collections.Counter(i.name for i in t.intros)
    p("Intros: %d  (%s)" % (len(t.intros), ", ".join("%s x%d" % kv for kv in names.most_common())))
    shown = t.intros if args.all_intros else [i for i in t.intros if i.name not in ("marker", "campath")]
    if not args.all_intros and len(shown) != len(t.intros):
        p("  (markers/campaths hidden, use --all-intros)")
    for i in shown:
        sp = "" if i.spectralPosition == (0, 0, 0) else "  spectral%s" % (i.spectralPosition,)
        p("  %-16s uid=%-4d intronum=%-4d pos=%s rot=%s flags=0x%x%s%s%s" % (
            i.name, i.UniqueID, i.intronum, i.position, i.rotation, i.flags & 0xFFFFFFFF, sp,
            " data" if i.has_data else "", " spline" if i.has_multiSpline else ""))

    p("")
    mv = t.morph_vertices
    p("Morph: %d MorphVertex, %d MorphColor, %d morphNormalIdx" % (
        len(mv), len(t.morph_colors), len(t.morph_normals)))
    if mv:
        same = sum(1 for m in mv if (m.hx, m.hy, m.hz) == (t.vertices[m.vindex].x, t.vertices[m.vindex].y,
                                                         t.vertices[m.vindex].z))
        mags = [abs(m.x) + abs(m.y) + abs(m.z) for m in mv]
        p("  hx,hy,hz == vertexList position for %d/%d (vertexList holds the material realm)" % (same, len(mv)))
        p("  spectral = h + (x,y,z); |delta|_1 max=%d median=%d" % (max(mags), statistics.median(mags)))
        for m in mv[:args.morph_samples]:
            p("    v%-5d material(%d,%d,%d) delta(%d,%d,%d)" % (m.vindex, m.hx, m.hy, m.hz, m.x, m.y, m.z))
    if t.morph_colors:
        diff = sum(1 for v, c in zip(t.vertices, t.morph_colors) if v.rgb15 != c)
        p("  MorphColor differs from TVertex.rgb15 on %d/%d vertices" % (diff, len(t.morph_colors)))
    if t.morph_normals:
        diff = sum(1 for f, n in zip(t.faces, t.morph_normals) if f.normal != n)
        p("  morphNormalIdx differs from TFace.normal on %d/%d faces" % (diff, len(t.morph_normals)))

    stats = [analyze.walk(tr) for tr in t.trees]
    owner, overlaps = analyze.face_coverage(L, stats)
    mat = analyze.material_positions(L)
    spec = analyze.spectral_positions(L)

    p("")
    p("BSP trees: %d   (faces owned by a leaf: %d/%d, overlaps %d)" % (
        len(t.trees), sum(o is not None for o in owner), t.numFaces, overlaps))
    for st in stats:
        tr = st.tree
        p("-" * 78)
        p("Tree %d: ID=%d flags=0x%04x splineID=%d globalOffset=%s localOffset=%s" % (
            tr.index, tr.ID, tr.flags & 0xFFFF, tr.splineID, tr.globalOffset, tr.localOffset))
        p("  nodes=%d leaves=%d (reachable from root: %d nodes, %d leaves), max depth=%d, null children=%d" % (
            len(tr.nodes), len(tr.leaves), st.reachable_nodes, st.reachable_leaves, st.max_depth,
            st.null_children))
        if st.leaf_depths:
            p("  leaf depth: min=%d mean=%.1f max=%d" % (
                min(st.leaf_depths), statistics.mean(st.leaf_depths), max(st.leaf_depths)))
            fpl = st.faces_per_leaf
            p("  faces/leaf: min=%d mean=%.1f median=%g max=%d total=%d" % (
                min(fpl), statistics.mean(fpl), statistics.median(fpl), max(fpl), sum(fpl)))
            p("  faces/leaf histogram: " + _hist(fpl, [0, 4, 8, 16, 32, 64]))
        if tr.nodes:
            nf = collections.Counter(n.flags for n in tr.nodes)
            p("  node flags: " + " ".join("0x%x:%d" % kv for kv in nf.items()))
        if tr.leaves:
            lf = collections.Counter(l.flags for l in tr.leaves)
            p("  leaf flags: " + " ".join("0x%x:%d" % kv for kv in lf.items()))
        if st.empty:
            alias = [o for o in t.trees if tr.bspRoot in o.leaf_at]
            p("  empty tree (bspRoot == startLeaves == endLeaves = 0x%x%s)" % (
                tr.bspRoot, ", aliases leaf %d of tree %d" % (
                    alias[0].leaf_at[tr.bspRoot].index, alias[0].index) if alias else ""))
        for prob in st.problems:
            p("  PROBLEM: " + prob)

        if not args.no_check:
            lc = analyze.check_leaves(L, tr, mat, spec)
            for realm in ("material", "spectral"):
                bc = lc[realm]
                if bc.count:
                    p("  leaf %-8s box==AABB %d/%d (+-1: %d), sphere encloses verts %d/%d (max overshoot %.1f)" % (
                        realm, bc.box_exact, bc.count, bc.box_within1, bc.sphere_contains, bc.count,
                        bc.max_overshoot))
            sp, er = analyze.check_nodes(L, st, mat, spec)
            for realm in ("material", "spectral"):
                bc = sp[realm]
                if bc.count:
                    p("  node %-8s sphere encloses subtree verts %d/%d (max overshoot %.1f)" % (
                        realm, bc.sphere_contains, bc.count, bc.max_overshoot))
            if er.count:
                p("  error fields vs. recomputed (+-1.5 / +-3.5 / max diff):")
                for k in analyze.ERROR_KEYS:
                    p("    %-15s %3d %3d / %d   max %.1f" % (k, er.within1[k], er.within3[k], er.count,
                                                            er.max_diff[k]))

        if args.nodes:
            for n in tr.nodes:
                p("  N%-4d @0x%05x plane(%d,%d,%d,d=%d) fl=%x front=%s back=%s sph=%s ssph=%s "
                  "err mat(f%d,b%d) spec(f%d,b%d)" % (
                      n.index, n.offset, n.a, n.b, n.c, n.d, n.flags,
                      _child(tr, n.front), _child(tr, n.back), _fmt_sph(n.sphere), _fmt_sph(n.spectralSphere),
                      n.front_material_error, n.back_material_error,
                      n.front_spectral_error, n.back_spectral_error))
            for lf in tr.leaves:
                p("  L%-4d @0x%05x faces %s+%d fl=%x sph=%s box=%s sbox=%s ssph=%s" % (
                    lf.index, lf.offset, lf.first_face, lf.numFaces, lf.flags, _fmt_sph(lf.sphere),
                    _fmt_box(lf.box), _fmt_box(lf.spectralBox), _fmt_sph(lf.spectralSphere)))
    return stats


def _child(tree, off):
    if off is None:
        return "-"
    if off in tree.leaf_at:
        return "L%d" % tree.leaf_at[off].index
    if off in tree.node_at:
        return "N%d" % tree.node_at[off].index
    return "?0x%x" % off


def to_json(L):
    t = L.terrain

    def tree_dict(tr):
        return {
            "index": tr.index, "ID": tr.ID, "flags": tr.flags, "splineID": tr.splineID,
            "globalOffset": tr.globalOffset, "localOffset": tr.localOffset,
            "nodes": [dict(asdict(n), front=_child(tr, n.front), back=_child(tr, n.back)) for n in tr.nodes],
            "leaves": [asdict(lf) for lf in tr.leaves],
            "root": _child(tr, tr.bspRoot),
        }

    return {
        "worldName": L.worldName, "streamUnitID": L.streamUnitID, "versionNumber": L.versionNumber,
        "flags": L.flags, "unitFlags": L.unitFlags, "waterZLevel": L.waterZLevel, "deathZ": L.deathZ,
        "bspPlaneError": L.bspPlaneError, "fog": L.fog,
        "terrain": {
            "numVertices": t.numVertices, "numFaces": t.numFaces, "numNormals": t.numNormals,
            "vertices": [asdict(v) for v in t.vertices],
            "faces": [asdict(f) for f in t.faces],
            "normals": t.normals,
            "portals": [asdict(pt) for pt in t.portals],
            "intros": [asdict(i) for i in t.intros],
            "morphVertices": [asdict(m) for m in t.morph_vertices],
            "morphColors": t.morph_colors,
            "morphNormalIdx": t.morph_normals,
            "bspTrees": [tree_dict(tr) for tr in t.trees],
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--disc", help="CD image (.img/.bin raw 2352 or .iso) containing BIGFILE.DAT")
    src.add_argument("--drm", nargs="+", help="level .drm file(s) already extracted")
    ap.add_argument("units", nargs="*", help="unit names to read from the disc, e.g. under1 city9")
    ap.add_argument("--bigfile", default="BIGFILE.DAT", help="name of the bigfile on the disc")
    ap.add_argument("--obj", metavar="DIR", help="write <unit>_material.obj and <unit>_spectral.obj")
    ap.add_argument("--scale", type=float, default=0.01, help="OBJ scale factor (default 0.01)")
    ap.add_argument("--no-textures", action="store_true", help="OBJ without UVs and texture pages")
    ap.add_argument("--crm", help="the unit's .crm (textures) when reading a --drm file")
    ap.add_argument("--layer", metavar="DIR", action="append", default=[],
                    help="also read files from DIR, after --mods (a prototype's maps: proto/<date>/areas)")
    ap.add_argument("--mods", metavar="MODS_DIR",
                    help="read units from the port's mods folder where they are there (edited units)")
    ap.add_argument("--json", metavar="PATH", help="write everything as JSON (file, or dir for several units)")
    ap.add_argument("--extract", metavar="DIR", help="also save the raw .drm read from the disc")
    ap.add_argument("--nodes", action="store_true", help="print every BSP node and leaf")
    ap.add_argument("--all-intros", action="store_true", help="also print marker/campath intros")
    ap.add_argument("--morph-samples", type=int, default=5, help="MorphVertex entries to print")
    ap.add_argument("--no-check", action="store_true", help="skip recomputing bounds from geometry")
    args = ap.parse_args(argv)

    inputs = []
    if args.mods:
        disc.OVERLAY = args.mods
    disc.LAYERS[:] = args.layer
    if args.disc:
        if not args.units:
            ap.error("give at least one unit name with --disc")
        bf = disc.BigFile(args.disc, args.bigfile)
        for u in args.units:
            inputs.append((u, bf.read_level(u)))
    else:
        for path in args.drm:
            with open(path, "rb") as f:
                inputs.append((os.path.splitext(os.path.basename(path))[0], f.read()))

    for unit, data in inputs:
        if args.extract:
            os.makedirs(args.extract, exist_ok=True)
            with open(os.path.join(args.extract, unit + ".drm"), "wb") as f:
                f.write(data)
        L = lvl.parse_level(data)
        report(L, unit, args)
        if args.obj:
            crm = None
            if args.disc and not args.no_textures:
                crm = bf.read_file(disc.level_path(unit).replace(".drm", ".crm"), unit)
            elif args.crm:
                with open(args.crm, "rb") as f:
                    crm = f.read()
            for path in objexport.export(L, args.obj, unit, args.scale, crm=crm):
                print("wrote", path)
        if args.json:
            path = args.json
            if len(inputs) > 1 or os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
                path = os.path.join(path, unit + ".json")
            with open(path, "w") as f:
                json.dump(to_json(L), f, indent=1)
            print("wrote", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
