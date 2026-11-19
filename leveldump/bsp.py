"""BSP compiler for terrain trees, and the engine's traversal for checking them.

A tree is built from a set of faces by recursive splits until a leaf holds at
most LEAF_MAX faces (the original data never exceeds 16 in practice). Faces are
never cut: each face goes to the side its centroid is on, and the node's plane
error fields record how far each child's geometry crosses the plane. This is
how the original trees were built (see README "Format notes"), and it means
collision stays correct for ANY choice of plane: the planes only decide how
well the tree prunes.

Errors are computed with the engine's own integer plane test
(dist = ((a,b,c) . p) >> 12 - d, as the GTE does in COLLIDE_PointAndTerrainFunc),
so they are exact for the quantised plane rather than approximate.

`traverse()` reimplements the engine's descent (node errors with its 5-unit
margin, leaf box test) so a tree can be checked against brute-force
segment/triangle intersection.
"""

import math
import random
from dataclasses import dataclass, field
from typing import List, Optional

from edit import bounding_sphere

LEAF_MAX = 16
COLLIDE_MARGIN = 5


def _dist(plane, p):
    a, b, c, d = plane
    return ((a * p[0] + b * p[1] + c * p[2]) >> 12) - d


@dataclass
class CNode:
    plane: tuple                     # (a, b, c, d)
    front: object = None
    back: object = None
    faces: List[int] = field(default_factory=list)   # all faces below


@dataclass
class CLeaf:
    faces: List[int]


def _quantise_plane(n, p):
    """Fixed-point plane through point p with unit normal n."""
    a, b, c = (int(round(x * 4096)) for x in n)
    a, b, c = (max(-32768, min(32767, v)) for v in (a, b, c))
    d = (a * p[0] + b * p[1] + c * p[2]) >> 12
    return (a, b, c, d)


class Compiler:
    def __init__(self, faces, pos, leaf_max=LEAF_MAX, candidates=64, seed=1, straddle_weight=2.0,
                 min_ratio=0.25):
        """`faces`: list of (v0, v1, v2); `pos`: vertex positions used to choose planes."""
        self.faces = faces
        self.pos = pos
        self.leaf_max = leaf_max
        self.candidates = candidates
        self.rng = random.Random(seed)
        self.straddle_weight = straddle_weight
        self.min_ratio = min_ratio
        self.centroid = []
        self.normal = []
        for v0, v1, v2 in faces:
            a, b, c = pos[v0], pos[v1], pos[v2]
            self.centroid.append(tuple((a[k] + b[k] + c[k]) / 3.0 for k in range(3)))
            u = [b[k] - a[k] for k in range(3)]
            w = [c[k] - a[k] for k in range(3)]
            n = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
            ln = math.sqrt(sum(x * x for x in n))
            self.normal.append(None if ln == 0 else tuple(x / ln for x in n))

    def build(self, face_ids):
        face_ids = list(face_ids)
        if len(face_ids) <= self.leaf_max:
            return CLeaf(face_ids)
        best = None
        for plane in self._candidate_planes(face_ids):
            front, back, straddle = self._classify(plane, face_ids)
            if not front or not back:
                continue
            n = len(face_ids)
            # Balance first (the original trees are close to perfectly
            # balanced), then few faces crossing the plane. Badly lopsided
            # splits are only taken when nothing else works.
            ratio = min(len(front), len(back)) / n
            score = abs(len(front) - len(back)) / n + self.straddle_weight * straddle / n
            if ratio < self.min_ratio:
                score += 10
            if best is None or score < best[0]:
                best = (score, plane, front, back)
        if best is None:
            plane, front, back = self._fallback(face_ids)
        else:
            _, plane, front, back = best
        node = CNode(plane, faces=face_ids)
        node.front = self.build(front)
        node.back = self.build(back)
        return node

    def _candidate_planes(self, face_ids):
        planes = []
        # face planes of a sample of the faces
        sample = face_ids if len(face_ids) <= self.candidates else self.rng.sample(face_ids, self.candidates)
        for f in sample:
            if self.normal[f] is not None:
                planes.append(_quantise_plane(self.normal[f], self.pos[self.faces[f][0]]))
        # axis-aligned planes through the median centroid
        for k in range(3):
            cs = sorted(self.centroid[f][k] for f in face_ids)
            m = cs[len(cs) // 2]
            n = [0.0, 0.0, 0.0]
            n[k] = 1.0
            p = [0, 0, 0]
            p[k] = int(math.floor(m))
            planes.append(_quantise_plane(n, p))
        return planes

    def _classify(self, plane, face_ids):
        front, back, straddle = [], [], 0
        a, b, c, d = plane
        for f in face_ids:
            ds = [_dist(plane, self.pos[v]) for v in self.faces[f]]
            if min(ds) >= 0:
                front.append(f)
            elif max(ds) <= 0:
                back.append(f)
            else:
                straddle += 1
                cen = self.centroid[f]
                (front if (a * cen[0] + b * cen[1] + c * cen[2]) / 4096.0 - d >= 0 else back).append(f)
        return front, back, straddle

    def _fallback(self, face_ids):
        """Split in half along the longest axis of the centroids (any split is valid)."""
        cs = [self.centroid[f] for f in face_ids]
        k = max(range(3), key=lambda i: max(c[i] for c in cs) - min(c[i] for c in cs))
        order = sorted(face_ids, key=lambda f: self.centroid[f][k])
        half = len(order) // 2
        n = [0.0, 0.0, 0.0]
        n[k] = 1.0
        p = [0, 0, 0]
        p[k] = int(math.floor(self.centroid[order[half]][k]))
        return _quantise_plane(n, p), order[half:], order[:half]


# --- turning a compiled tree into records ----------------------------------

@dataclass
class NodeRec:
    plane: tuple
    flags: int
    front: tuple                 # ("N", index) or ("L", index)
    back: tuple
    sphere: tuple
    spectralSphere: tuple
    errors: tuple                # front_spectral, back_spectral, front_material, back_material


@dataclass
class LeafRec:
    first: int                   # index into the tree's new face order
    count: int
    flags: int
    sphere: tuple
    box: tuple
    spectralBox: tuple
    spectralSphere: tuple


def _bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def flatten(root, faces, mat, spec):
    """Pre-order records (root first, front before back, as in the original data).

    Returns (nodes, leaves, face_order): face_order lists the tree's faces in
    their new storage order, leaf after leaf."""
    nodes, leaves, order = [], [], []

    def verts(face_ids):
        vs = set()
        for f in face_ids:
            vs.update(faces[f])
        return vs

    def leaf_faces(n):
        return n.faces if isinstance(n, CLeaf) else n.faces

    def visit(n):
        if isinstance(n, CLeaf):
            vs = verts(n.faces)
            first = len(order)
            order.extend(n.faces)
            if vs:
                sm = bounding_sphere([mat[v] for v in vs])
                ss = bounding_sphere([spec[v] for v in vs])
                rec = LeafRec(first, len(n.faces), 0x2, sm[0] + (sm[1],), _bbox([mat[v] for v in vs]),
                              _bbox([spec[v] for v in vs]), ss[0] + (ss[1],))
            else:
                rec = LeafRec(first, 0, 0x2, (0, 0, 0, 0), (0,) * 6, (0,) * 6, (0, 0, 0, 0))
            leaves.append(rec)
            return ("L", len(leaves) - 1)
        idx = len(nodes)
        nodes.append(None)
        f = visit(n.front)
        b = visit(n.back)
        vs = verts(n.faces)
        fv = verts(leaf_faces(n.front))
        bv = verts(leaf_faces(n.back))
        errs = []
        for pos in (spec, mat):
            fe = max([_dist(n.plane, pos[v]) for v in bv] + [0])
            be = min([_dist(n.plane, pos[v]) for v in fv] + [0])
            errs += [min(fe, 32767), max(be, -32768)]
        sm = bounding_sphere([mat[v] for v in vs])
        ss = bounding_sphere([spec[v] for v in vs])
        nodes[idx] = NodeRec(n.plane, 0x1, f, b, sm[0] + (sm[1],), ss[0] + (ss[1],), tuple(errs))
        return ("N", idx)

    visit(root)
    return nodes, leaves, order


# --- the engine's traversal, for checking -----------------------------------

def traverse(tree, new_pos, old_pos, spectral=False, slack=0):
    """Leaves the engine would test for a segment (COLLIDE_PointAndTerrainFunc).

    `tree` is a level.BSPTree; positions are tree-local."""
    out = []
    if tree.bspRoot is None or (not tree.nodes and not tree.leaves):
        return out
    stack = [tree.bspRoot]
    while stack:
        off = stack.pop()
        if off in tree.leaf_at:
            lf = tree.leaf_at[off]
            bx = lf.spectralBox if spectral else lf.box
            lo = (bx.minx, bx.miny, bx.minz)
            hi = (bx.maxx, bx.maxy, bx.maxz)
            if any((new_pos[k] > hi[k] + slack and old_pos[k] > hi[k] + slack) or
                   (new_pos[k] < lo[k] - slack and old_pos[k] < lo[k] - slack) for k in range(3)):
                continue
            out.append(lf)
            continue
        n = tree.node_at[off]
        plane = (n.a, n.b, n.c, n.d)
        x = _dist(plane, new_pos)
        y = _dist(plane, old_pos)
        fe = (n.front_spectral_error if spectral else n.front_material_error) + COLLIDE_MARGIN
        be = (n.back_spectral_error if spectral else n.back_material_error) - COLLIDE_MARGIN
        if y >= fe:
            if fe >= x:
                if n.back is not None:
                    stack.append(n.back)
            if n.front is not None:
                stack.append(n.front)
        elif y <= be:
            if x >= be and n.front is not None:
                stack.append(n.front)
            if n.back is not None:
                stack.append(n.back)
        else:
            for c in (n.back, n.front):
                if c is not None:
                    stack.append(c)
    return out


def segment_hits_triangle(p, q, a, b, c):
    """Exact-ish (float) test whether segment p-q crosses triangle abc."""
    def sub(u, v):
        return (u[0] - v[0], u[1] - v[1], u[2] - v[2])

    def cross(u, v):
        return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])

    def dot(u, v):
        return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]

    n = cross(sub(b, a), sub(c, a))
    dp, dq = dot(n, sub(p, a)), dot(n, sub(q, a))
    if (dp > 0 and dq > 0) or (dp < 0 and dq < 0) or dp == dq:
        return False
    t = dp / (dp - dq)
    x = tuple(p[k] + (q[k] - p[k]) * t for k in range(3))
    for u, v in ((a, b), (b, c), (c, a)):
        if dot(cross(sub(v, u), sub(x, u)), n) < 0:
            return False
    return True


def check_segments(level, tree_index, segments, spectral=False):
    """For each segment, every face it really crosses must be in a leaf the
    traversal visits. Returns the number of misses."""
    import analyze
    t = level.terrain
    tr = t.trees[tree_index]
    pos = analyze.spectral_positions(level) if spectral else analyze.material_positions(level)
    faces = []
    for lf in tr.leaves:
        if lf.first_face is not None:
            faces += range(lf.first_face, lf.first_face + lf.numFaces)
    misses = 0
    for p, q in segments:
        visited = set()
        for lf in traverse(tr, q, p, spectral):
            visited.update(range(lf.first_face, lf.first_face + lf.numFaces))
        for f in faces:
            fc = t.faces[f]
            if f not in visited and segment_hits_triangle(p, q, pos[fc.v0], pos[fc.v1], pos[fc.v2]):
                misses += 1
    return misses


def random_segments(level, tree_index, count, seed=7, spectral=False):
    """Short segments around the tree's geometry: drops onto faces and random moves."""
    import analyze
    rng = random.Random(seed)
    t = level.terrain
    tr = t.trees[tree_index]
    pos = analyze.spectral_positions(level) if spectral else analyze.material_positions(level)
    faces = [f for lf in tr.leaves if lf.first_face is not None
             for f in range(lf.first_face, lf.first_face + lf.numFaces)]
    segs = []
    for _ in range(count):
        fc = t.faces[rng.choice(faces)]
        w = [rng.random() for _ in range(3)]
        s = sum(w)
        pt = [sum(pos[v][k] * w[i] / s for i, v in enumerate((fc.v0, fc.v1, fc.v2))) for k in range(3)]
        if rng.random() < 0.5:   # a fall through the surface
            segs.append(((int(pt[0]), int(pt[1]), int(pt[2] + 200)), (int(pt[0]), int(pt[1]), int(pt[2] - 200))))
        else:                     # a random step across it
            dvec = [rng.uniform(-300, 300) for _ in range(3)]
            segs.append((tuple(int(pt[k] + dvec[k]) for k in range(3)), tuple(int(pt[k] - dvec[k]) for k in range(3))))
    return segs


# --- rebuilding a level's trees ---------------------------------------------

def rebuild_level(data, leaf_max=LEAF_MAX, trees=None, membership=None):
    """Recompile the BSP trees of a level .drm (all non-empty trees, or the
    indices in `trees`) and return (new bytes, stats).

    Each tree keeps its faces (faces are only reordered within the tree's own
    slice of faceList, together with morphNormalIdx), its header fields and its
    place in memory; empty trees stay empty and keep aliasing what follows.

    `membership` ({tree index: (first face, count)}) gives each tree's faces
    explicitly, for a level whose face list was rebuilt (geometry.py); the old
    leaves are then ignored. A tree given 0 faces becomes empty."""
    import struct
    import analyze
    from drm import Drm
    from level import SIZEOF_TFACE, parse_level

    L = parse_level(data)
    d = Drm(data)
    t = L.terrain
    mat = analyze.material_positions(L)
    spec = analyze.spectral_positions(L)
    face_tuples = [(f.v0, f.v1, f.v2) for f in t.faces]

    live = [tr for tr in t.trees if tr.nodes or tr.leaves]
    region_start = min(tr.bspRoot for tr in live)
    region_end = max(tr.endLeaves for tr in live)

    perm = list(range(t.numFaces))          # new index -> old index
    built = {}
    stats = []
    for tr in t.trees:
        if membership is not None:
            first, count = membership.get(tr.index, (0, 0))
            tree_faces = list(range(first, first + count))
        else:
            if not (tr.nodes or tr.leaves):
                continue
            slices = sorted((lf.first_face, lf.numFaces) for lf in tr.leaves if lf.first_face is not None)
            tree_faces = [f for first, n in slices for f in range(first, first + n)]
        if not tree_faces:
            continue
        lo = min(tree_faces)
        if sorted(tree_faces) != list(range(lo, lo + len(tree_faces))):
            raise ValueError("tree %d's faces are not one contiguous slice" % tr.index)
        if trees is not None and tr.index not in trees:
            built[tr.index] = None
            continue
        comp = Compiler(face_tuples, mat, leaf_max=leaf_max)
        root = comp.build(tree_faces)
        nodes, leaves, order = flatten(root, face_tuples, mat, spec)
        for i, f in enumerate(order):
            perm[lo + i] = f
        built[tr.index] = (lo, nodes, leaves)
        depth = _depth(root)
        stats.append({"tree": tr.index, "faces": len(tree_faces), "nodes": len(nodes),
                      "leaves": len(leaves), "max_depth": depth,
                      "old_nodes": len(tr.nodes), "old_leaves": len(tr.leaves)})

    # lay out the region: trees in index order, nodes then leaves
    blob = bytearray()
    ptrs = []                   # (offset in blob, kind, value spec)
    headers = {}
    for tr in t.trees:
        if tr.index in built and built[tr.index] is None:
            # not rebuilt: copy its records unchanged
            raise NotImplementedError("partial rebuilds are not supported yet")
        if tr.index not in built:
            off = len(blob)
            headers[tr.index] = (off, off, off)
            continue
        lo, nodes, leaves = built[tr.index]
        root_off = len(blob)
        leaves_off = root_off + 0x2C * len(nodes)
        end_off = leaves_off + 0x30 * len(leaves)

        def ref(r):
            return root_off + 0x2C * r[1] if r[0] == "N" else leaves_off + 0x30 * r[1]

        for n in nodes:
            o = len(blob)
            blob += struct.pack("<hhhHhhhhiIIhhhHhhhh", *n.sphere, n.plane[0], n.plane[1], n.plane[2], n.flags,
                                n.plane[3], 0, 0, *n.spectralSphere, *n.errors)
            ptrs.append((o + 0x14, "region", ref(n.front)))
            ptrs.append((o + 0x18, "region", ref(n.back)))
        for lf in leaves:
            o = len(blob)
            blob += struct.pack("<hhhHIhh6h6hhhhH", *lf.sphere, 0, lf.count, lf.flags, *lf.box, *lf.spectralBox,
                                *lf.spectralSphere)
            if lf.count:
                ptrs.append((o + 0x08, "face", lo + lf.first))
        # a single-leaf tree has bspRoot == startLeaves
        headers[tr.index] = (root_off if nodes else leaves_off, leaves_off, end_off)
    # empty trees placed before a later tree alias that tree's start, as in the original
    hdr_fields = [t.ptrs["BSPTreeArray"] + i * 0x24 + k for i in range(t.numBSPTrees) for k in (0, 4, 8)]
    delta = d.replace(region_start, region_end, bytes(blob), [o for o, _, _ in ptrs], allow_into=hdr_fields)

    # everything behind the region moved by delta: read the offsets again
    # (from the Terrain header; the trees can't be parsed until they're written)
    terrain_off = d.ptr(0x00)
    tptr = {"faceList": d.ptr(terrain_off + 0x20), "BSPTreeArray": d.ptr(terrain_off + 0x48),
            "morphNormalIdx": d.ptr(terrain_off + 0x4C)}
    face_base = tptr["faceList"]
    for o, kind, v in ptrs:
        val = region_start + v if kind == "region" else face_base + v * SIZEOF_TFACE
        d.pack("I", region_start + o, val)
    tree_arr = tptr["BSPTreeArray"]
    for i, (root, start, end) in headers.items():
        d.pack("III", tree_arr + i * 0x24, region_start + root, region_start + start, region_start + end)

    # reorder faces (and the spectral normal index that goes with each face)
    old_faces = [bytes(d.body[face_base + i * SIZEOF_TFACE: face_base + (i + 1) * SIZEOF_TFACE])
                 for i in range(t.numFaces)]
    for new, old in enumerate(perm):
        d.body[face_base + new * SIZEOF_TFACE: face_base + (new + 1) * SIZEOF_TFACE] = old_faces[old]
    if tptr["morphNormalIdx"] is not None:
        mo = tptr["morphNormalIdx"]
        old_mn = [d.u16(mo + 2 * i) for i in range(t.numFaces)]
        for new, old in enumerate(perm):
            d.pack("H", mo + 2 * new, old_mn[old])
    return d.to_bytes(), {"trees": stats, "region_delta": delta,
                          "faces_moved": sum(1 for i, o in enumerate(perm) if i != o)}


def _depth(n):
    if isinstance(n, CLeaf):
        return 0
    return 1 + max(_depth(n.front), _depth(n.back))
