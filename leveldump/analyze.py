"""BSP walking and consistency checks of the stored bounds against the geometry.

Coordinates: terrain vertices are local to the BSP tree that owns their faces;
world position = vertex + BSPTree.globalOffset (COLLIDE_* subtracts globalOffset
from the query point, RelocateLevel moves globalOffset, not the vertices).

Plane test (COLLIDE_PointAndTerrainFunc): dist = ((a,b,c) . p) >> 12 - d, with the
normal (a,b,c) in 1.3.12 fixed point (|n| ~ 4096).
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List

from level import BSP_FLAG_LEAF, Level, BSPTree


def material_positions(level: Level):
    t = level.terrain
    return [(v.x, v.y, v.z) for v in t.vertices]


def spectral_positions(level: Level):
    """MORPH_UpdateNormals: vertex = (hx + x, hy + y, hz + z) for each MorphVertex."""
    pos = material_positions(level)
    for mv in level.terrain.morph_vertices:
        pos[mv.vindex] = (mv.hx + mv.x, mv.hy + mv.y, mv.hz + mv.z)
    return pos


def plane_dist(node, p):
    # GTE: (rotation matrix rows = positions) * (a,b,c) >> 12, then minus d
    return ((node.a * p[0] + node.b * p[1] + node.c * p[2]) >> 12) - node.d


def plane_dist_f(node, p):
    return (node.a * p[0] + node.b * p[1] + node.c * p[2]) / 4096.0 - node.d


@dataclass
class TreeStats:
    tree: BSPTree
    reachable_nodes: int = 0
    reachable_leaves: int = 0
    max_depth: int = 0
    leaf_depths: List[int] = field(default_factory=list)
    faces_per_leaf: List[int] = field(default_factory=list)
    null_children: int = 0
    face_ranges: List[tuple] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    node_faces: Dict[int, list] = field(default_factory=dict)  # node offset -> face indices beneath
    empty: bool = False  # bspRoot == startLeaves == endLeaves


def walk(tree: BSPTree) -> TreeStats:
    st = TreeStats(tree)
    if tree.bspRoot is None:
        return st
    if not tree.nodes and not tree.leaves:
        # Empty tree: all three pointers are equal and point just past the
        # previous tree's leaves, i.e. at whatever follows (usually the next
        # tree's first leaf). There is nothing of its own to walk.
        st.empty = True
        return st
    seen = set()

    def visit(off, depth):
        if off in seen:
            st.problems.append("node 0x%x reached twice" % off)
            return []
        seen.add(off)
        st.max_depth = max(st.max_depth, depth)
        if off in tree.leaf_at:
            leaf = tree.leaf_at[off]
            if not leaf.flags & BSP_FLAG_LEAF:
                st.problems.append("leaf %d lacks flag 0x2" % leaf.index)
            st.reachable_leaves += 1
            st.leaf_depths.append(depth)
            st.faces_per_leaf.append(leaf.numFaces)
            if leaf.first_face is None:
                return []
            st.face_ranges.append((leaf.first_face, leaf.numFaces))
            return list(range(leaf.first_face, leaf.first_face + leaf.numFaces))
        node = tree.node_at.get(off)
        if node is None:
            st.problems.append("pointer 0x%x is neither node nor leaf of this tree" % off)
            return []
        if node.flags & BSP_FLAG_LEAF:
            st.problems.append("node %d has leaf flag" % node.index)
        st.reachable_nodes += 1
        faces = []
        for child in (node.front, node.back):
            if child is None:
                st.null_children += 1
            else:
                faces += visit(child, depth + 1)
        st.node_faces[off] = faces
        return faces

    visit(tree.bspRoot, 0)
    if st.reachable_nodes != len(tree.nodes):
        st.problems.append("%d of %d nodes reachable" % (st.reachable_nodes, len(tree.nodes)))
    if st.reachable_leaves != len(tree.leaves):
        st.problems.append("%d of %d leaves reachable" % (st.reachable_leaves, len(tree.leaves)))
    return st


def face_coverage(level: Level, stats: List[TreeStats]):
    """Which faces are referenced by which tree; returns (owner list, overlaps)."""
    owner = [None] * level.terrain.numFaces
    overlaps = 0
    for st in stats:
        for first, n in st.face_ranges:
            for f in range(first, first + n):
                if f >= len(owner):
                    continue          # a leaf of a level whose face list was emptied
                if owner[f] is not None:
                    overlaps += 1
                owner[f] = st.tree.index
    return owner, overlaps


def _verts_of(level, faces):
    t = level.terrain
    vs = set()
    for f in faces:
        fc = t.faces[f]
        vs.update((fc.v0, fc.v1, fc.v2))
    return vs


def _bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


@dataclass
class BoundCheck:
    """Aggregated comparison of stored bounds vs. bounds recomputed from vertices."""
    count: int = 0
    box_exact: int = 0
    box_within1: int = 0          # every coordinate within 1 unit
    box_contains: int = 0
    box_contains1: int = 0        # contains the vertices, allowing 1 unit (original spectral boxes)
    sphere_contains: int = 0      # every vertex within radius (+1 for rounding)
    sphere_center_is_box_center: int = 0
    sphere_center_is_mean: int = 0
    max_overshoot: float = 0.0    # max(vertex dist - radius)
    radius_slack: List[float] = field(default_factory=list)  # radius - tight radius about the stored center


def _sphere_check(bc: BoundCheck, sph, pts):
    c = (sph.x, sph.y, sph.z)
    far = max(math.dist(c, p) for p in pts)
    bc.max_overshoot = max(bc.max_overshoot, far - sph.radius)
    if far <= sph.radius + 1:
        bc.sphere_contains += 1
    bc.radius_slack.append(sph.radius - far)
    bb = _bbox(pts)
    bcen = tuple((bb[i] + bb[i + 3]) // 2 for i in range(3))
    if all(abs(bcen[i] - c[i]) <= 1 for i in range(3)):
        bc.sphere_center_is_box_center += 1
    mean = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
    if all(abs(mean[i] - c[i]) <= 1 for i in range(3)):
        bc.sphere_center_is_mean += 1


def check_leaves(level: Level, tree: BSPTree, mat, spec):
    res = {"material": BoundCheck(), "spectral": BoundCheck()}
    t = level.terrain
    for leaf in tree.leaves:
        if not leaf.numFaces or leaf.first_face is None:
            continue
        vs = _verts_of(level, range(leaf.first_face, leaf.first_face + leaf.numFaces))
        for key, pos, box, sph in (("material", mat, leaf.box, leaf.sphere),
                                   ("spectral", spec, leaf.spectralBox, leaf.spectralSphere)):
            pts = [pos[v] for v in vs]
            bc = res[key]
            bc.count += 1
            bb = _bbox(pts)
            stored = (box.minx, box.miny, box.minz, box.maxx, box.maxy, box.maxz)
            if bb == stored:
                bc.box_exact += 1
            if all(abs(bb[i] - stored[i]) <= 1 for i in range(6)):
                bc.box_within1 += 1
            if all(stored[i] <= bb[i] for i in range(3)) and all(stored[i] >= bb[i] for i in range(3, 6)):
                bc.box_contains += 1
            if all(stored[i] <= bb[i] + 1 for i in range(3)) and all(stored[i] >= bb[i] - 1 for i in range(3, 6)):
                bc.box_contains1 += 1
            _sphere_check(bc, sph, pts)
    return res


ERROR_KEYS = ("front_material", "back_material", "front_spectral", "back_spectral")


@dataclass
class ErrorCheck:
    count: int = 0
    within1: Dict[str, int] = field(default_factory=lambda: {k: 0 for k in ERROR_KEYS})
    within3: Dict[str, int] = field(default_factory=lambda: {k: 0 for k in ERROR_KEYS})
    max_diff: Dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in ERROR_KEYS})
    worst: List[tuple] = field(default_factory=list)


def check_nodes(level: Level, st: TreeStats, mat, spec):
    """Compare node spheres and the four error fields with the geometry below the node.

    Rule tested (from the way COLLIDE uses them):
      front_*_error = max(0, max signed distance over vertices of faces in the BACK subtree)
                      (how far back geometry pokes out in front of the plane)
      back_*_error  = min(0, min signed distance over vertices of faces in the FRONT subtree)
                      (how far front geometry pokes out behind the plane)
    so a point with dist >= front_error only needs the front child, dist <= back_error
    only the back child (COLLIDE adds a 5-unit margin on top).
    """
    tree = st.tree
    spheres = {"material": BoundCheck(), "spectral": BoundCheck()}
    err = ErrorCheck()
    for node in tree.nodes:
        if node.offset not in st.node_faces:
            continue
        faces = st.node_faces[node.offset]
        vs = _verts_of(level, faces)
        if vs:
            for key, pos, sph in (("material", mat, node.sphere), ("spectral", spec, node.spectralSphere)):
                spheres[key].count += 1
                _sphere_check(spheres[key], sph, [pos[v] for v in vs])
        front = _verts_of(level, st.node_faces.get(node.front, _leaf_faces(tree, node.front)))
        back = _verts_of(level, st.node_faces.get(node.back, _leaf_faces(tree, node.back)))
        calc = {}
        for key, pos in (("material", mat), ("spectral", spec)):
            calc["front_" + key] = max([plane_dist_f(node, pos[v]) for v in back] + [0.0])
            calc["back_" + key] = min([plane_dist_f(node, pos[v]) for v in front] + [0.0])
        stored = {
            "front_material": node.front_material_error, "back_material": node.back_material_error,
            "front_spectral": node.front_spectral_error, "back_spectral": node.back_spectral_error,
        }
        err.count += 1
        for k, v in calc.items():
            diff = abs(v - stored[k])
            if diff <= 1.5:
                err.within1[k] += 1
            if diff <= 3.5:
                err.within3[k] += 1
            if diff > err.max_diff[k]:
                err.max_diff[k] = diff
            if diff > 3.5:
                err.worst.append((node.index, k, stored[k], round(v, 1)))
    return spheres, err


def _leaf_faces(tree, off):
    leaf = tree.leaf_at.get(off)
    if leaf is None or leaf.first_face is None:
        return []
    return list(range(leaf.first_face, leaf.first_face + leaf.numFaces))
