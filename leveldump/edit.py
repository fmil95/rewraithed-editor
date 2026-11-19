"""Geometry edits on a level that keep its layout (same vertices, faces and BSP).

Moving vertices invalidates data the engine derived from them; this rebuilds it
the way the original tools did (see README "Format notes"):

* face normals (material: TFace.normal, spectral: morphNormalIdx) for every
  face that touches a moved vertex, reusing an identical normal from the pool
  or appending new ones (the normal list grows; `Drm.insert` fixes pointers);
* MorphVertex.h (= material position) for morphing vertices; their spectral
  delta is kept unless a spectral position is given;
* BSPLeaf box / spectralBox / sphere / spectralSphere for leaves with a changed face;
* BSPNode sphere / spectralSphere and the four plane error fields for every
  node above a changed face. Splitting planes are kept; the error fields grow
  to cover geometry that now crosses them.
"""

import math

import analyze
from drm import Drm
from level import (SIZEOF_MORPHVERTEX, SIZEOF_NORMAL, SIZEOF_TFACE, SIZEOF_TVERTEX,
                   encode_normal, parse_level, signed16)

S16 = (-32768, 32767)


def _check16(v, what):
    if not S16[0] <= v <= S16[1]:
        raise ValueError("%s = %d does not fit in a signed 16-bit value" % (what, v))
    return v


def _rgb15(r, g, b):
    """8-bit RGB to the game's 15-bit colour (r in bits 0-4, g 5-9, b 10-14)."""
    return (r >> 3) | ((g >> 3) << 5) | ((b >> 3) << 10)


def bounding_sphere(points):
    """Ritter-style sphere, then the radius made tight around the rounded centre."""
    pts = list(points)
    x = pts[0]
    y = max(pts, key=lambda p: math.dist(x, p))
    z = max(pts, key=lambda p: math.dist(y, p))
    c = [(y[k] + z[k]) / 2 for k in range(3)]
    r = math.dist(y, z) / 2
    for p in pts:
        d = math.dist(c, p)
        if d > r:
            nr = (r + d) / 2
            c = [c[k] + (p[k] - c[k]) * (nr - r) / d for k in range(3)]
            r = nr
    ci = tuple(int(round(v)) for v in c)
    ri = int(math.ceil(max(math.dist(ci, p) for p in pts)))
    return ci, min(ri, 0xFFFF)


def _bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _face_normal(pos, fc):
    a, b, c = pos[fc.v0], pos[fc.v1], pos[fc.v2]
    u = [b[k] - a[k] for k in range(3)]
    v = [c[k] - a[k] for k in range(3)]
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    ln = math.sqrt(sum(x * x for x in n))
    return None if ln == 0 else tuple(x / ln for x in n)


class LevelEditor:
    def __init__(self, data):
        self.drm = Drm(data)
        self.level = parse_level(data)
        t = self.level.terrain
        self.material = analyze.material_positions(self.level)
        self.spectral = analyze.spectral_positions(self.level)
        self.morph_index = {m.vindex: i for i, m in enumerate(t.morph_vertices)}
        self.colors = {}            # vertex -> (r, g, b) material colour
        self.spectral_colors = {}   # vertex -> (r, g, b) spectral colour
        self.warnings = []
        self.summary = {}

    # --- edits --------------------------------------------------------------

    def move_vertex(self, v, material=None, spectral=None):
        """Set new positions (tree-local game units); a vertex without morph data
        has one position for both realms, so `spectral` is then ignored unless
        it equals the material one."""
        if material is not None:
            old_m, old_s = self.material[v], self.spectral[v]
            self.material[v] = tuple(int(c) for c in material)
            if v in self.morph_index:
                if spectral is None:   # keep the spectral delta
                    self.spectral[v] = tuple(self.material[v][k] + old_s[k] - old_m[k] for k in range(3))
            else:
                self.spectral[v] = self.material[v]
        if spectral is not None:
            spectral = tuple(int(c) for c in spectral)
            if v in self.morph_index:
                self.spectral[v] = spectral
            elif spectral != self.spectral[v]:
                self.warnings.append("vertex %d has no morph data; its spectral position follows the material one" % v)

    def set_color(self, v, rgb, spectral_rgb=None):
        self.colors[v] = rgb
        self.spectral_colors[v] = rgb if spectral_rgb is None else spectral_rgb

    # --- rebuild ------------------------------------------------------------

    def build(self):
        """Apply everything and return the new .drm bytes."""
        L = self.level
        t = L.terrain
        orig_mat = analyze.material_positions(L)
        orig_spec = analyze.spectral_positions(L)
        moved = {v for v in range(t.numVertices)
                 if self.material[v] != orig_mat[v] or self.spectral[v] != orig_spec[v]}
        for v in moved:
            for k in range(3):
                _check16(self.material[v][k], "vertex %d" % v)
                _check16(self.spectral[v][k], "vertex %d spectral" % v)
        faces = {i for i, f in enumerate(t.faces) if f.v0 in moved or f.v1 in moved or f.v2 in moved}

        # normals -----------------------------------------------------------
        pool = list(t.normals)
        lookup = {}
        for i, n in enumerate(pool):
            lookup.setdefault(n, i)
        new_normal = {}
        new_morph_normal = {}
        orig_count = len(pool)

        def normal_index(n_unit, old_index):
            old = pool[abs(old_index)]
            sign = -1 if old_index < 0 else 1
            old_vec = (old[0] * sign, old[1] * sign, old[2] * sign)
            if sum(n_unit[k] * old_vec[k] for k in range(3)) < 0:
                n_unit = tuple(-x for x in n_unit)   # keep the face's original facing
            comp = tuple(int(round(x * 4096)) for x in n_unit)
            axis = max(range(3), key=lambda k: abs(n_unit[k]))
            key = comp + (axis,)
            if key in lookup:
                return lookup[key]
            neg = (-comp[0], -comp[1], -comp[2], axis)
            if lookup.get(neg, 0) > 0:
                return -lookup[neg]
            pool.append(key)
            lookup[key] = len(pool) - 1
            return len(pool) - 1

        has_morph_normals = bool(t.morph_normals)
        for fi in faces:
            fc = t.faces[fi]
            n = _face_normal(self.material, fc)
            if n is not None:
                new_normal[fi] = normal_index(n, signed16(fc.normal))
            if has_morph_normals:
                n = _face_normal(self.spectral, fc)
                if n is not None:
                    new_morph_normal[fi] = normal_index(n, signed16(t.morph_normals[fi]))
        if len(pool) > 32767:
            raise ValueError("too many normals (%d)" % len(pool))

        added = len(pool) - orig_count
        if added:
            at = t.ptrs["normalList"] + orig_count * SIZEOF_NORMAL
            self.drm.insert(at, (added * SIZEOF_NORMAL + 3) & ~3)
            # everything behind the normal list moved: parse again for offsets
            self.level = L = _reparse(self.drm)
            t = L.terrain
        d = self.drm
        d.pack("i", t.offset + 0x18, len(pool))
        for i in range(orig_count, len(pool)):
            d.pack("hhh", t.ptrs["normalList"] + i * SIZEOF_NORMAL, *encode_normal(*pool[i]))

        # vertices, colours, morph data -----------------------------------
        for v in moved:
            d.pack("hhh", t.ptrs["vertexList"] + v * SIZEOF_TVERTEX, *self.material[v])
            if v in self.morph_index:
                o = t.ptrs["MorphDiffList"] + self.morph_index[v] * SIZEOF_MORPHVERTEX
                m, s = self.material[v], self.spectral[v]
                d.pack("7h", o, _check16(s[0] - m[0], "delta"), _check16(s[1] - m[1], "delta"),
                       _check16(s[2] - m[2], "delta"), v, m[0], m[1], m[2])
        for v, (r, g, b) in self.colors.items():
            o = t.ptrs["vertexList"] + v * SIZEOF_TVERTEX
            d.pack("H", o + 6, _rgb15(r, g, b))
            d.pack("BBB", o + 8, r & 0xF8, g & 0xF8, b & 0xF8)
        if t.ptrs["MorphColorList"] is not None:
            for v, (r, g, b) in self.spectral_colors.items():
                d.pack("H", t.ptrs["MorphColorList"] + v * 2, _rgb15(r, g, b))

        for fi, n in new_normal.items():
            d.pack("h", t.ptrs["faceList"] + fi * SIZEOF_TFACE + 8, n)
        for fi, n in new_morph_normal.items():
            d.pack("h", t.ptrs["morphNormalIdx"] + fi * 2, n)

        # BSP bounds ----------------------------------------------------------
        leaves_done = nodes_done = 0
        mat, spec = self.material, self.spectral
        for tr in t.trees:
            st = analyze.walk(tr)
            for leaf in tr.leaves:
                if leaf.first_face is None or not leaf.numFaces:
                    continue
                fr = range(leaf.first_face, leaf.first_face + leaf.numFaces)
                if not any(f in faces for f in fr):
                    continue
                vs = _verts(t, fr)
                for pos, sph_off, box_off in ((mat, 0x00, 0x10), (spec, 0x28, 0x1C)):
                    pts = [pos[v] for v in vs]
                    c, r = bounding_sphere(pts)
                    d.pack("hhhH", leaf.offset + sph_off, c[0], c[1], c[2], r)
                    d.pack("6h", leaf.offset + box_off, *_bbox(pts))
                leaves_done += 1
            for node in tr.nodes:
                under = st.node_faces.get(node.offset)
                if not under or not any(f in faces for f in under):
                    continue
                vs = _verts(t, under)
                front = _verts(t, st.node_faces.get(node.front, analyze._leaf_faces(tr, node.front)))
                back = _verts(t, st.node_faces.get(node.back, analyze._leaf_faces(tr, node.back)))
                errs = {}
                for key, pos, sph_off in (("material", mat, 0x00), ("spectral", spec, 0x1C)):
                    c, r = bounding_sphere([pos[v] for v in vs])
                    d.pack("hhhH", node.offset + sph_off, c[0], c[1], c[2], r)
                    f_err = max([analyze.plane_dist_f(node, pos[v]) for v in back] + [0.0])
                    b_err = min([analyze.plane_dist_f(node, pos[v]) for v in front] + [0.0])
                    errs[key] = (min(int(math.ceil(f_err)), 32767), max(int(math.floor(b_err)), -32768))
                d.pack("hhhh", node.offset + 0x24, errs["spectral"][0], errs["spectral"][1],
                       errs["material"][0], errs["material"][1])
                nodes_done += 1

        self.summary = {
            "moved_vertices": len(moved), "changed_faces": len(faces),
            "normals_added": added, "leaves_rebuilt": leaves_done, "nodes_rebuilt": nodes_done,
            "recoloured_vertices": len(self.colors),
        }
        return d.to_bytes()


def _verts(t, faces):
    vs = set()
    for f in faces:
        fc = t.faces[f]
        vs.update((fc.v0, fc.v1, fc.v2))
    return vs


def _reparse(d: Drm):
    return parse_level(d.to_bytes())
