"""Wavefront OBJ export of a level's terrain (one file per realm).

Each BSP tree becomes an OBJ group/object; its faces are emitted with the
tree's globalOffset applied, since terrain vertices are stored local to the
tree that owns their faces. Vertex colours use the common `v x y z r g b`
extension that Blender imports as a colour attribute.

The game is Z-up like Blender, so axes are written as-is; positions are
multiplied by `scale` (a unit spans thousands to tens of thousands of game units).
Faces not referenced by any BSP leaf go into an "unowned" object.

Next to each OBJ, `<file>.obj.map.json` lists the level vertex behind every OBJ
vertex of every object; levelpatch.py uses it to read an edited OBJ back.
"""

import json
import os

import analyze


def _rgb_material(v):
    return v.r0 / 255.0, v.g0 / 255.0, v.b0 / 255.0


def _rgb15(c):
    # MORPH_UpdateNormals: r = (c & 0x1f) << 3, g = (c >> 2) & 0xf8, b = (c >> 7) & 0xf8
    return ((c & 0x1F) << 3) / 255.0, ((c >> 2) & 0xF8) / 255.0, ((c >> 7) & 0xF8) / 255.0


def write_obj(path, level, realm, scale=0.01, owner=None, portals=True, mtl=None):
    """`mtl`: .mtl file name to reference; faces then get UVs and a material
    per (texture page, palette) (textures.py)."""
    t = level.terrain
    if realm == "material":
        pos = analyze.material_positions(level)
        colors = [_rgb_material(v) for v in t.vertices]
    else:
        pos = analyze.spectral_positions(level)
        if t.morph_colors:
            colors = [_rgb15(c) for c in t.morph_colors]
        else:
            colors = [_rgb_material(v) for v in t.vertices]

    if owner is None:
        stats = [analyze.walk(tr) for tr in t.trees]
        owner, _ = analyze.face_coverage(level, stats)

    vmap = {"unit": level.worldName, "realm": realm, "scale": scale, "objects": {}}
    groups = {}
    for fi, tree_index in enumerate(owner):
        groups.setdefault(tree_index, []).append(fi)

    tex_entries = None
    if mtl:
        import textures
        tex_entries = textures.texture_entries(level)
    vt_next = [1]

    with open(path, "w", newline="\n") as f:
        f.write("# %s, %s realm, exported by rewraithed-editor leveldump\n" % (level.worldName, realm))
        if mtl:
            f.write("mtllib %s\n" % mtl)
        f.write("# scale %g, Z up, %d vertices %d faces\n" % (scale, t.numVertices, t.numFaces))
        next_index = 1
        order = sorted(groups, key=lambda k: (k is None, k if k is not None else 0))
        for tree_index in order:
            faces = groups[tree_index]
            if tree_index is None:
                name, off = "unowned", (0, 0, 0)
            else:
                tr = t.trees[tree_index]
                name = "tree%d_id%d_flags%04x" % (tr.index, tr.ID, tr.flags & 0xFFFF)
                off = tr.globalOffset
            # Double-sided surfaces are stored as two faces on the same three
            # vertices with opposite winding (the second one's normal index is
            # negated). OBJ importers such as Blender's merge faces that share a
            # vertex set, so second copies go to a separate "_backfaces" object.
            front, back, seen = [], [], set()
            for fi in faces:
                fc = t.faces[fi]
                if fc.v0 == fc.v1 or fc.v1 == fc.v2 or fc.v0 == fc.v2:
                    continue
                key = frozenset((fc.v0, fc.v1, fc.v2))
                (back if key in seen else front).append(fi)
                seen.add(key)
            for suffix, part in (("", front), ("_backfaces", back)):
                if not part:
                    continue
                used = sorted({v for fi in part for v in (t.faces[fi].v0, t.faces[fi].v1, t.faces[fi].v2)})
                remap = {}
                f.write("o %s%s\n" % (name, suffix))
                vmap["objects"][name + suffix] = {"offset": list(off), "vertices": used}
                for v in used:
                    p = pos[v]
                    r, g, b = colors[v]
                    f.write("v %.4f %.4f %.4f %.4f %.4f %.4f\n" % (
                        (p[0] + off[0]) * scale, (p[1] + off[1]) * scale, (p[2] + off[2]) * scale, r, g, b))
                    remap[v] = next_index
                    next_index += 1
                current = None
                for fi in part:
                    fc = t.faces[fi]
                    tex = textures.face_texture(level, tex_entries, fc) if tex_entries is not None else None
                    if tex is None:
                        if mtl and current != "untextured":
                            f.write("usemtl untextured\n")
                            current = "untextured"
                        f.write("f %d %d %d\n" % (remap[fc.v0], remap[fc.v1], remap[fc.v2]))
                        continue
                    mname = textures.material_name(tex[0], tex[1])
                    if mname != current:
                        f.write("usemtl %s\n" % mname)
                        current = mname
                    for u, v in tex[2]:
                        f.write("vt %.6f %.6f\n" % (u / 256.0, 1.0 - v / 256.0))
                    t0 = vt_next[0]
                    vt_next[0] += 3
                    f.write("f %d/%d %d/%d %d/%d\n" % (remap[fc.v0], t0, remap[fc.v1], t0 + 1, remap[fc.v2], t0 + 2))

        if portals and t.portals:
            # StreamUnitPortal.t1 / t2 are the two triangles of the portal quad.
            for i, p in enumerate(t.portals):
                f.write("o portal%d_%s\n" % (i, p.tolevelname.replace(",", "_sig")))
                for tri in (p.t1, p.t2):
                    for vx in tri:
                        f.write("v %.4f %.4f %.4f\n" % (vx[0] * scale, vx[1] * scale, vx[2] * scale))
                f.write("f %d %d %d\n" % (next_index, next_index + 1, next_index + 2))
                f.write("f %d %d %d\n" % (next_index + 3, next_index + 4, next_index + 5))
                next_index += 6
    # Which level vertex each OBJ vertex is, so an edited OBJ can be applied
    # back with levelpatch.py (OBJ files themselves carry no ids).
    with open(path + ".map.json", "w") as f:
        json.dump(vmap, f)
    return path


def export(level, out_dir, unit, scale=0.01, crm=None):
    """With `crm` (the unit's .crm bytes) the OBJs get UVs, materials and page images."""
    os.makedirs(out_dir, exist_ok=True)
    stats = [analyze.walk(tr) for tr in level.terrain.trees]
    owner, _ = analyze.face_coverage(level, stats)
    paths = []
    mtl = None
    if crm is not None:
        import textures
        entries = textures.texture_entries(level)
        used = set()
        for fc in level.terrain.faces:
            tex = textures.face_texture(level, entries, fc)
            if tex is not None:
                used.add((tex[0], tex[1]))
        mtl = textures.write_pages(out_dir, unit, level, crm, used)
        with open(os.path.join(out_dir, mtl), "a", newline="\n") as f:
            f.write("newmtl untextured\nKd 0.5 0.5 0.5\n")
        paths.append(os.path.join(out_dir, mtl))
    for realm in ("material", "spectral"):
        p = os.path.join(out_dir, "%s_%s.obj" % (unit, realm))
        write_obj(p, level, realm, scale, owner, mtl=mtl)
        paths.append(p)
    if level.terrain.intros:
        import intros
        paths.append(intros.write_intros_obj(os.path.join(out_dir, "%s_intros.obj" % unit), level, scale))
    return paths
