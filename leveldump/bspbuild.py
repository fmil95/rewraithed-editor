#!/usr/bin/env python3
"""Recompile a level's BSP trees and check the result.

    python bspbuild.py --disc KAIN2.img under1 --install <exe folder>/mods
    python bspbuild.py --drm edited.drm -o rebuilt.drm --segments 2000

The checks run before anything is written:
  * every face is in exactly one leaf, nodes and leaves are all reachable;
  * leaf boxes equal the AABB of their vertices, spheres enclose them;
  * the plane error fields follow the rule the engine relies on;
  * random segments through the geometry: every face a segment really crosses
    is in a leaf that the engine's own traversal (bsp.traverse) visits.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze  # noqa: E402
import bsp  # noqa: E402
import disc  # noqa: E402
import level as lvl  # noqa: E402
from levelpatch import verify  # noqa: E402


def check(data, segments, label):
    L, bad = verify(data)
    t = L.terrain
    stats = [analyze.walk(tr) for tr in t.trees]
    owner, overlaps = analyze.face_coverage(L, stats)
    if owner.count(None) or overlaps:
        bad.append("%d faces without a leaf, %d in two leaves" % (owner.count(None), overlaps))
    misses = 0
    for tr in t.trees:
        if not tr.leaves:
            continue
        for spectral in (False, True):
            segs = bsp.random_segments(L, tr.index, segments, spectral=spectral)
            misses += bsp.check_segments(L, tr.index, segs, spectral=spectral)
    print("%s: %d check problems, %d segment misses (%d segments per tree and realm)"
          % (label, len(bad), misses, segments))
    for b in bad:
        print("  ", b)
    return L, bad, misses


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--disc", help="CD image containing BIGFILE.DAT")
    src.add_argument("--drm", help="level .drm file")
    ap.add_argument("unit", nargs="?", help="unit name (with --disc)")
    ap.add_argument("--leaf-max", type=int, default=bsp.LEAF_MAX, help="faces per leaf (default 16)")
    ap.add_argument("--segments", type=int, default=500, help="random test segments per tree and realm")
    ap.add_argument("-o", "--output", help="output .drm")
    ap.add_argument("--install", metavar="MODS_DIR", help="write to MODS_DIR/kain2/area/<area>/bin/<unit>.drm")
    args = ap.parse_args(argv)

    if args.disc:
        if not args.unit:
            ap.error("give the unit name with --disc")
        unit = args.unit
        data = disc.BigFile(args.disc).read_level(unit)
    else:
        with open(args.drm, "rb") as f:
            data = f.read()
        # the level knows its own unit name; the file may be called anything
        unit = lvl.parse_level(data).worldName or os.path.splitext(os.path.basename(args.drm))[0]

    out, info = bsp.rebuild_level(data, leaf_max=args.leaf_max)
    for s in info["trees"]:
        print("tree %(tree)d: %(faces)d faces -> %(nodes)d nodes, %(leaves)d leaves, depth %(max_depth)d "
              "(was %(old_nodes)d nodes, %(old_leaves)d leaves)" % s)
    print("BSP region %+d bytes, %d faces reordered, size %d -> %d" % (
        info["region_delta"], info["faces_moved"], len(data), len(out)))

    _, bad, misses = check(out, args.segments, "rebuilt")
    if bad or misses:
        print("not written")
        return 1

    paths = []
    if args.output:
        paths.append(args.output)
    if args.install:
        paths.append(os.path.join(args.install, *disc.level_path(unit).strip("\\").split("\\")))
    for p in paths:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "wb") as f:
            f.write(out)
        print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
