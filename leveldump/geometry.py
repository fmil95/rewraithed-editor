"""Edits that add or delete terrain geometry, then a BSP rebuild.

Input is an OBJ exported by leveldump (plus its .map.json) and edited in Blender:
vertices may be moved, added or deleted, faces added or deleted, and new
objects added.

* Vertices: original vertices keep their indices, because morph entries and
  VMObjects (animated water etc.) refer to them by index. A deleted vertex just
  stays in the list unused; new vertices are appended at the end. OBJ vertices
  are matched to the level by index when the object only gained vertices
  (Blender appends new ones), or by exact position when some were deleted.
  A vertex both moved and part of an object that lost vertices can't be told
  apart from a new one; it then becomes a new vertex (reported).
* Faces: the face list is rebuilt from the OBJ, tree by tree (OBJ object
  "tree<N>_..." -> tree N; "_backfaces" objects belong to the same tree; new
  objects go to tree 0). A face with the same three vertices in the same cyclic
  order as an original face keeps that face's record (flags, texture, sort push).
  A new face copies those from the original face of its tree that shares most
  vertices with it, else the nearest one, so new faces get a neighbour's
  texture (stretched: texture coordinates aren't edited yet).
* New vertices take their colour from the OBJ if it has colours, else from the
  nearest original vertex; their spectral position equals the material one
  (no morph entry) and their spectral colour is the nearest original vertex's.
* Then normals are recomputed for new or changed faces, the vertex, colour,
  face and spectral-normal lists are resized, and every BSP tree is recompiled
  (bsp.rebuild_level).
"""

import math
import re
import struct

import analyze
import bsp
from drm import Drm
from edit import NormalPool, _face_normal, _rgb15
from level import SIZEOF_MORPHVERTEX, SIZEOF_TFACE, SIZEOF_TVERTEX, parse_level, signed16


def read_obj_full(path):
    """{object: {"v": [(x, y, z, colour or None)], "f": [[local vertex indices]],
    "fm": [material name or None per face], "fuv": [[(u, v) or None per corner]]}}.

    `has_uv` in the result tells whether the file had any texture coordinates."""
    objs = {}
    cur = None
    mat = None
    gverts = []           # global index -> (object, local index)
    vts = []

    def new(name):
        objs[name] = {"v": [], "f": [], "fm": [], "fuv": []}

    with open(path) as f:
        for line in f:
            if line.startswith("o "):
                cur = line[2:].strip()
                if cur not in objs:
                    new(cur)
            elif line.startswith("v "):
                if cur is None:
                    cur = ""
                    new(cur)
                p = [float(x) for x in line.split()[1:]]
                gverts.append((cur, len(objs[cur]["v"])))
                objs[cur]["v"].append((p[0], p[1], p[2], tuple(p[3:6]) if len(p) >= 6 else None))
            elif line.startswith("vt "):
                p = [float(x) for x in line.split()[1:3]]
                vts.append((p[0], p[1]))
            elif line.startswith("usemtl "):
                mat = line[7:].strip()
            elif line.startswith("f "):
                idx, uvs = [], []
                for tok in line.split()[1:]:
                    parts = tok.split("/")
                    i = int(parts[0])
                    i = i - 1 if i > 0 else len(gverts) + i
                    idx.append(gverts[i])
                    if len(parts) > 1 and parts[1]:
                        j = int(parts[1])
                        uvs.append(vts[j - 1 if j > 0 else len(vts) + j])
                    else:
                        uvs.append(None)
                owner = idx[0][0]
                if any(o != owner for o, _ in idx):
                    raise SystemExit("a face in %s uses vertices of two objects" % path)
                objs[owner]["f"].append([li for _, li in idx])
                objs[owner]["fm"].append(mat)
                objs[owner]["fuv"].append(uvs)
    return objs


def obj_has_uv(objs):
    return any(uv is not None for o in objs.values() for face in o["fuv"] for uv in face)


def _canon(tri):
    """Rotation of a triangle that starts at its smallest index (keeps winding)."""
    i = tri.index(min(tri))
    return tri[i:] + tri[:i]


TREE_NAME = re.compile(r"^tree(\d+)_")


class TopologyEdit:
    def __init__(self, data):
        self.data = data
        self.L = parse_level(data)
        t = self.L.terrain
        self.t = t
        self.orig_mat = analyze.material_positions(self.L)
        self.orig_spec = analyze.spectral_positions(self.L)
        self.morph_index = {m.vindex: i for i, m in enumerate(t.morph_vertices)}
        stats = [analyze.walk(tr) for tr in t.trees]
        self.owner, _ = analyze.face_coverage(self.L, stats)
        self.warnings = []
        self.report = {}

    def apply_obj(self, obj_path, map_path, use_colors=True):
        import json
        with open(map_path) as f:
            vmap = json.load(f)
        if vmap.get("realm", "material") != "material":
            raise SystemExit("geometry edits come from the material-realm OBJ")
        scale = vmap["scale"]
        objs = read_obj_full(obj_path)
        t = self.t

        mat = list(self.orig_mat)            # level index -> material position
        colours = {}                         # level index -> 8-bit colour from the OBJ
        new_verts = []                       # [(tree, position, colour or None)]
        new_key = {}                         # (tree, position) -> level index
        tree_faces = {tr.index: [] for tr in t.trees}   # tree -> [(v0, v1, v2)]
        moved = set()
        moved_lost = 0

        def tree_of(name):
            m = TREE_NAME.match(name)
            return int(m.group(1)) if m else None

        offsets = {}
        for name, info in vmap["objects"].items():
            ti = tree_of(name)
            if ti is not None:
                offsets[ti] = info["offset"]

        handled = set()
        for name in objs:
            obj = objs[name]
            info = vmap["objects"].get(name)
            ti = tree_of(name)
            if ti is None:
                if name.startswith("portal"):
                    continue           # stream portal quads are not terrain
                ti = 0
                self.warnings.append("new object %r added to tree 0" % name)
            if ti >= len(t.trees):
                raise SystemExit("object %r names tree %d, the level has %d trees" % (name, ti, len(t.trees)))
            handled.add(name)
            off = info["offset"] if info else offsets.get(ti, [0, 0, 0])
            local = [(int(round(x / scale)) - off[0], int(round(y / scale)) - off[1],
                      int(round(z / scale)) - off[2]) for x, y, z, _ in obj["v"]]
            orig_ids = info["vertices"] if info else []
            ids = self._match(local, orig_ids)
            if info and len(local) < len(orig_ids):
                # vertices were deleted: moved ones can't be identified
                kept = {i for i in ids if i is not None}
                moved_lost += sum(1 for i in orig_ids if i not in kept)
            level_ids = []
            for k, p in enumerate(local):
                v = ids[k]
                col = obj["v"][k][3]
                if v is None:
                    key = (ti, p)
                    if key not in new_key:
                        new_key[key] = t.numVertices + len(new_verts)
                        new_verts.append((ti, p, col))
                    v = new_key[key]
                else:
                    if p != self.orig_mat[v]:
                        if v in moved and mat[v] != p:
                            self.warnings.append("vertex %d was moved differently in two objects" % v)
                        else:
                            mat[v] = p
                            moved.add(v)
                    if col is not None and use_colors:
                        colours.setdefault(v, col)
                level_ids.append(v)
            for pi, poly in enumerate(obj["f"]):
                vs = [level_ids[i] for i in poly]
                uvs = obj["fuv"][pi]
                for j in range(1, len(vs) - 1):       # fan triangulation
                    corners = (0, j, j + 1)
                    tri = tuple(vs[c] for c in corners)
                    if len(set(tri)) == 3:
                        uv = {vs[c]: uvs[c] for c in corners} if all(uvs[c] is not None for c in corners) else None
                        tree_faces[ti].append((tri, obj["fm"][pi], uv))
        missing = [n for n in vmap["objects"] if n not in handled]
        for n in missing:
            self.warnings.append("object %r is missing from the OBJ: its faces are deleted" % n)

        self.mat = mat
        self.colours = colours
        self.new_verts = new_verts
        self.tree_faces = tree_faces
        self.moved = moved
        self.report.update({"moved_vertices": len(moved), "new_vertices": len(new_verts),
                            "unidentified_after_delete": moved_lost})

    def _match(self, local, orig_ids):
        """Level vertex index (or None for new) for each OBJ vertex of one object."""
        K, M = len(orig_ids), len(local)
        if K == 0:
            return [None] * M
        by_pos = {}
        for v in orig_ids:
            by_pos.setdefault(self.orig_mat[v], []).append(v)
        # position matching
        pool = {k: list(v) for k, v in by_pos.items()}
        pos_ids = []
        for p in local:
            lst = pool.get(p)
            pos_ids.append(lst.pop(0) if lst else None)
        pos_hits = sum(1 for v in pos_ids if v is not None)
        if M >= K:
            # only additions (Blender appends new vertices): match by index,
            # which also keeps the identity of moved vertices
            prefix = list(orig_ids) + [None] * (M - K)
            prefix_hits = sum(1 for k in range(K) if local[k] == self.orig_mat[orig_ids[k]])
            if prefix_hits + max(4, K // 20) >= pos_hits:
                return prefix
        return pos_ids

    # --- writing ----------------------------------------------------------

    def build(self, leaf_max=bsp.LEAF_MAX):
        t = self.t
        d = Drm(self.data)
        nv_old, nf_old = t.numVertices, t.numFaces
        mat = self.mat + [p for _, p, _ in self.new_verts]
        nv_new = len(mat)
        if nv_new > 0xFFFF:
            raise SystemExit("too many vertices (%d)" % nv_new)
        for p in mat:
            for c in p:
                if not -32768 <= c <= 32767:
                    raise SystemExit("vertex position %r out of range" % (p,))

        # spectral positions: moved morph vertices keep their delta, others follow
        spec = list(mat)
        for v, i in self.morph_index.items():
            om, os_ = self.orig_mat[v], self.orig_spec[v]
            spec[v] = tuple(mat[v][k] + os_[k] - om[k] for k in range(3))

        # nearest original vertex of the same tree, for new vertices' attributes
        tree_verts = {}
        for fi, f in enumerate(t.faces):
            if self.owner[fi] is not None:
                tree_verts.setdefault(self.owner[fi], set()).update((f.v0, f.v1, f.v2))
        all_orig = list(range(nv_old))

        def nearest(ti, p):
            cands = tree_verts.get(ti) or all_orig
            return min(cands, key=lambda v: (self.orig_mat[v][0] - p[0]) ** 2 + (self.orig_mat[v][1] - p[1]) ** 2 +
                       (self.orig_mat[v][2] - p[2]) ** 2)

        # faces: reuse records, else inherit from a neighbour
        orig_by_canon = {}
        vert_faces = {}
        for fi, f in enumerate(t.faces):
            ti = self.owner[fi]
            orig_by_canon.setdefault((ti, _canon((f.v0, f.v1, f.v2))), []).append(fi)
            for v in (f.v0, f.v1, f.v2):
                vert_faces.setdefault(v, []).append(fi)
        centroid = [tuple(sum(self.orig_mat[v][k] for v in (f.v0, f.v1, f.v2)) / 3.0 for k in range(3))
                    for f in t.faces]
        faces_of_tree = {}
        for fi, ti in enumerate(self.owner):
            faces_of_tree.setdefault(ti, []).append(fi)

        def donor(ti, tri, p):
            score = {}
            for v in tri:
                for fi in vert_faces.get(v, ()):
                    if self.owner[fi] == ti:
                        score[fi] = score.get(fi, 0) + 1
            if score:
                best = max(score.values())
                cands = [fi for fi, s in score.items() if s == best]
            else:
                cands = faces_of_tree.get(ti) or list(range(nf_old))
            return min(cands, key=lambda fi: sum((centroid[fi][k] - p[k]) ** 2 for k in range(3)))

        pool = NormalPool(t.normals)
        texpool = TexturePool(self.L)
        new_faces = []            # (v0, v1, v2, attr, sortPush, normal, textoff)
        new_morph = []
        membership = {}
        reused = inherited = 0
        moved = self.moved
        for tr in t.trees:
            ti = tr.index
            first = len(new_faces)
            for tri, mname, uv in self.tree_faces.get(ti, []):
                key = (ti, _canon(tri))
                old = orig_by_canon.get(key)
                if old:
                    fi = old.pop(0)
                    f = t.faces[fi]
                    rec = [f.v0, f.v1, f.v2, f.attr, f.sortPush, signed16(f.normal), f.textoff]
                    mn = signed16(t.morph_normals[fi]) if t.morph_normals else None
                    if any(v in moved for v in tri):
                        n = _face_normal(mat, f)
                        if n is not None:
                            rec[5] = pool.index(n, rec[5])
                        if mn is not None:
                            n = _face_normal(spec, f)
                            if n is not None:
                                mn = pool.index(n, mn)
                    reused += 1
                else:
                    cen = tuple(sum(mat[v][k] for v in tri) / 3.0 for k in range(3))
                    fi = donor(ti, tri, cen)
                    f = t.faces[fi]
                    fake = type("F", (), {"v0": tri[0], "v1": tri[1], "v2": tri[2]})
                    n = _face_normal(mat, fake)
                    nidx = pool.index(n) if n is not None else signed16(f.normal)
                    mn = None
                    if t.morph_normals:
                        ns = _face_normal(spec, fake)
                        mn = pool.index(ns) if ns is not None else nidx
                    rec = [tri[0], tri[1], tri[2], f.attr, f.sortPush, nidx, f.textoff]
                    inherited += 1
                rec[6] = texpool.textoff(rec, mname, uv, f)
                new_faces.append(rec)
                new_morph.append(mn)
            membership[ti] = (first, len(new_faces) - first)

        # --- vertices, colours, morph h -----------------------------------
        def terrain():
            return d.ptr(0x00)

        def tptr(off):
            return d.ptr(terrain() + off)

        vl = tptr(0x1C)
        for v in moved:
            d.pack("hhh", vl + v * SIZEOF_TVERTEX, *mat[v])
        for v, col in self.colours.items():
            r, g, b = (int(round(c * 255)) for c in col)
            vx = t.vertices[v]
            if max(abs(r - vx.r0), abs(g - vx.g0), abs(b - vx.b0)) > 8:
                o = vl + v * SIZEOF_TVERTEX
                d.pack("H", o + 6, _rgb15(r, g, b))
                d.pack("BBB", o + 8, r & 0xF8, g & 0xF8, b & 0xF8)
        mdl = tptr(0x3C)
        if mdl is not None:
            for v, i in self.morph_index.items():
                if v in moved:
                    o = mdl + i * SIZEOF_MORPHVERTEX
                    m, s = mat[v], spec[v]
                    d.pack("7h", o, s[0] - m[0], s[1] - m[1], s[2] - m[2], v, m[0], m[1], m[2])

        if self.new_verts:
            recs = bytearray()
            spectral_cols = []
            for ti, p, col in self.new_verts:
                near = t.vertices[nearest(ti, p)]
                if col is not None:
                    r, g, b = (int(round(c * 255)) for c in col)
                else:
                    r, g, b = near.r0, near.g0, near.b0
                recs += struct.pack("<hhhHBBBB", p[0], p[1], p[2], _rgb15(r, g, b), r & 0xF8, g & 0xF8,
                                    b & 0xF8, near.code)
                nidx = nearest(ti, p)
                spectral_cols.append(t.morph_colors[nidx] if t.morph_colors else _rgb15(r, g, b))
            d.insert(vl + nv_old * SIZEOF_TVERTEX, len(recs))
            vl = tptr(0x1C)
            d.body[vl + nv_old * SIZEOF_TVERTEX: vl + nv_new * SIZEOF_TVERTEX] = recs
            mcl = tptr(0x40)
            if mcl is not None:
                extra = struct.pack("<%dH" % len(spectral_cols), *spectral_cols)
                d.insert(mcl + nv_old * 2, (len(extra) + 3) & ~3)
                mcl = tptr(0x40)
                d.body[mcl + nv_old * 2: mcl + nv_old * 2 + len(extra)] = extra
        d.pack("i", terrain() + 0x10, nv_new)

        # --- texture entries, normals, faces, spectral normals -------------
        added_textures = texpool.write(d)
        added_normals = pool.write(d, _TerrainView(d))
        fl = tptr(0x20)
        fbytes = b"".join(struct.pack("<HHHBbhH", *r) for r in new_faces)
        leaf_ptrs = self._leaf_pointer_locations(d)
        d.replace(fl, fl + nf_old * SIZEOF_TFACE, fbytes, allow_into=leaf_ptrs)
        mni = tptr(0x4C)
        if mni is not None:
            old_len = nf_old * 2
            new_len = len(new_faces) * 2
            while (new_len - old_len) % 4:
                new_len += 2
            vals = [(m if m is not None else 0) & 0xFFFF for m in new_morph]
            mbytes = struct.pack("<%dH" % len(vals), *vals) + b"\0" * (new_len - 2 * len(vals))
            # morphNormalIdx may start or end on a half word: replace whole words
            if old_len % 4 == 0 and mni % 4 == 0:
                d.replace(mni, mni + old_len, mbytes)
            else:
                self._replace_unaligned(d, mni, old_len, mbytes, allow=[terrain() + 0x4C])
        d.pack("i", terrain() + 0x14, len(new_faces))

        out, info = bsp.rebuild_level(d.to_bytes(), leaf_max=leaf_max, membership=membership)
        self.report.update({"faces": "%d -> %d" % (nf_old, len(new_faces)), "faces_kept": reused,
                            "faces_new": inherited, "vertices": "%d -> %d" % (nv_old, nv_new),
                            "normals_added": added_normals, "texture_entries_added": added_textures,
                            "faces_retextured": texpool.changed})
        return out, info

    def _leaf_pointer_locations(self, d):
        """Locations of every BSPLeaf.faceList pointer (rewritten by the BSP rebuild)."""
        out = []
        t_off = d.ptr(0)
        n = d.s32(t_off + 0x44)
        arr = d.ptr(t_off + 0x48)
        for i in range(n):
            o = arr + i * 0x24
            start, end = d.ptr(o + 4), d.ptr(o + 8)
            if start is None or end is None:
                continue
            for lo in range(start, end, 0x30):
                out.append(lo + 8)
        return out

    @staticmethod
    def _replace_unaligned(d, start, old_len, new_bytes, allow=()):
        """morphNormalIdx may start or end on a half word: swap the whole words
        around it. Its own start offset doesn't change, so the Terrain pointer
        to it (listed in `allow`) stays valid."""
        end = start + old_len
        a = start & ~3
        b = (end + 3) & ~3
        head = bytes(d.body[a:start])
        tail = bytes(d.body[end:b])
        data = head + new_bytes + tail
        while len(data) % 4:
            data += b"\0"
        d.replace(a, b, data, allow_into=allow)


class TexturePool:
    """TextureFT3 entries: keep a face's entry when its material and UVs match it,
    reuse an identical entry, else append one (the list grows at
    EndTextureList; textoff is a 16-bit byte offset, so about 5,400 entries max)."""

    def __init__(self, level):
        import textures
        self.entries = textures.texture_entries(level)
        self.orig_count = len(self.entries)
        self.lookup = {}
        for i, e in enumerate(self.entries):
            self.lookup.setdefault(e, i)
        self.changed = 0

    def textoff(self, rec, mname, uv, src_face):
        """Byte offset for face record `rec` (v0, v1, v2, ..., textoff) given the
        OBJ material and {vertex: (u, v)}; `src_face` is the original face the
        record came from (kept) or its donor (new)."""
        import textures
        if mname == "untextured":
            if rec[6] != 0xFFFF:
                self.changed += 1
            return 0xFFFF
        key = textures.parse_material(mname)
        if key is None or uv is None:
            return rec[6]
        tpage, clut = key

        def q(x):
            return max(0, min(255, int(round(x * 256))))

        uvq = [(q(uv[v][0]), q(1.0 - uv[v][1])) for v in rec[:3]]
        attr = 0
        old = None
        if src_face.textoff != 0xFFFF and src_face.textoff % 12 == 0 and src_face.textoff // 12 < self.orig_count:
            old = self.entries[src_face.textoff // 12]
            attr = old[8]
        entry = (uvq[0][0], uvq[0][1], clut, uvq[1][0], uvq[1][1], tpage, uvq[2][0], uvq[2][1], attr)
        cur = rec[6]
        if cur != 0xFFFF and cur % 12 == 0 and cur // 12 < len(self.entries) and self.entries[cur // 12] == entry:
            return rec[6]
        self.changed += 1
        if entry not in self.lookup:
            self.entries.append(entry)
            self.lookup[entry] = len(self.entries) - 1
        off = self.lookup[entry] * 12
        if off > 0xFFF0:
            raise SystemExit("too many texture entries")
        return off

    def write(self, d):
        added = len(self.entries) - self.orig_count
        if not added:
            return 0
        t_off = d.ptr(0)
        end = d.ptr(t_off + 0x38)
        d.insert(end, added * 12)       # EndTextureList (== end) moves to the new end
        for i in range(self.orig_count, len(self.entries)):
            d.pack("BBHBBHBBH", end + (i - self.orig_count) * 12, *self.entries[i])
        return added


class _TerrainView:
    """What NormalPool.write needs from a Terrain, read live from the Drm."""

    def __init__(self, d):
        self.offset = d.ptr(0)
        self.ptrs = {"normalList": d.ptr(self.offset + 0x24)}
