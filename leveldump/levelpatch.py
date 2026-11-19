#!/usr/bin/env python3
"""Write a modified level .drm: vertex moves and colours from an edited OBJ, or a scripted test edit.

The level keeps its vertices, faces and BSP structure; what depends on vertex
positions (normals, BSP spheres, boxes and plane error fields) is rebuilt
(see edit.py). Adding or deleting geometry needs a BSP compiler and is not
supported yet.

Workflow with Blender:
    python leveldump.py --disc KAIN2.img under1 --obj work/
    (Blender: import work/under1_material.obj with Z up / Y forward, move
     vertices in Edit Mode without adding or deleting any, then
     File > Export > Wavefront (.obj) with Z up / Y forward, "Selection Only"
     off, "Apply Modifiers" off, "Triangulated Mesh" off, Colors on
     if you painted vertex colours)
    python levelpatch.py --disc KAIN2.img under1 --material-obj edited.obj \\
        --map work/under1_material.obj.map.json --install <exe folder>/mods

Scripted test edit (no Blender):
    python levelpatch.py --disc KAIN2.img under1 --hill 1500 0 900 400 --tint 255 60 40 \\
        --install <exe folder>/mods

--install writes <dir>/kain2/area/<area>/bin/<unit>.drm, the path the port
loads in place of the file in BIGFILE.DAT.
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze  # noqa: E402
import disc  # noqa: E402
import edit  # noqa: E402
import level as lvl  # noqa: E402


def read_obj(path):
    """{object name: [(x, y, z, colour or None), ...]} in file order."""
    objs, cur = {}, None
    with open(path) as f:
        for line in f:
            if line.startswith("o "):
                cur = line[2:].strip()
                objs.setdefault(cur, [])
            elif line.startswith("v "):
                if cur is None:
                    cur = ""
                    objs.setdefault(cur, [])
                p = [float(x) for x in line.split()[1:]]
                objs[cur].append((p[0], p[1], p[2], tuple(p[3:6]) if len(p) >= 6 else None))
    return objs


def apply_obj(ed, obj_path, map_path, realm, use_colors):
    with open(map_path) as f:
        vmap = json.load(f)
    scale = vmap["scale"]
    objs = read_obj(obj_path)
    proposals = {}
    colours = {}
    for name, info in vmap["objects"].items():
        verts = objs.get(name)
        if verts is None:
            # Blender may write "name_meshname"; accept an object that starts with the name
            cands = [k for k in objs if k.split("_backfaces")[0] == name or k.startswith(name + "_")]
            cands = [k for k in cands if len(objs[k]) == len(info["vertices"])]
            if len(cands) != 1:
                raise SystemExit("object %r from the map is missing in %s (renamed or joined?)" % (name, obj_path))
            verts = objs[cands[0]]
        if len(verts) != len(info["vertices"]):
            raise SystemExit("object %r has %d vertices, the level has %d: vertices were added or deleted"
                             % (name, len(verts), len(info["vertices"])))
        off = info["offset"]
        for (x, y, z, col), v in zip(verts, info["vertices"]):
            p = (int(round(x / scale)) - off[0], int(round(y / scale)) - off[1], int(round(z / scale)) - off[2])
            proposals.setdefault(v, []).append(p)
            if col is not None:
                colours.setdefault(v, []).append(tuple(int(round(c * 255)) for c in col))
    cur = ed.material if realm == "material" else ed.spectral
    moved = 0
    for v, ps in proposals.items():
        changed = [p for p in ps if p != cur[v]]
        if not changed:
            continue
        if len(set(changed)) > 1:
            ed.warnings.append("vertex %d was moved differently in two objects; using the first" % v)
        moved += 1
        if realm == "material":
            ed.move_vertex(v, material=changed[0])
        else:
            ed.move_vertex(v, spectral=changed[0])
    recol = 0
    if use_colors:
        t = ed.level.terrain
        for v, cs in colours.items():
            vx = t.vertices[v]
            old = (vx.r0, vx.g0, vx.b0) if realm == "material" else _rgb15_to_8(t.morph_colors[v]) \
                if t.morph_colors else (vx.r0, vx.g0, vx.b0)
            new = cs[0]
            if max(abs(new[k] - old[k]) for k in range(3)) > 8:   # ignore 15-bit rounding
                if realm == "material":
                    sp = _rgb15_to_8(t.morph_colors[v]) if t.morph_colors else new
                    ed.set_color(v, new, ed.spectral_colors.get(v, sp))
                else:
                    ed.set_color(v, ed.colors.get(v, (vx.r0, vx.g0, vx.b0)), new)
                recol += 1
    return moved, recol


def _rgb15_to_8(c):
    return ((c & 0x1F) << 3, (c >> 2) & 0xF8, (c >> 7) & 0xF8)


def apply_hill(ed, cx, cy, radius, height, tint=None):
    """Raise (or lower) every vertex within `radius` of (cx, cy) by a smooth bump."""
    t = ed.level.terrain
    n = 0
    for v in range(t.numVertices):
        x, y, z = ed.material[v]
        r = math.hypot(x - cx, y - cy)
        if r >= radius:
            continue
        w = 0.5 * (1 + math.cos(math.pi * r / radius))
        ed.move_vertex(v, material=(x, y, z + int(round(height * w))))
        if tint is not None:
            vx = t.vertices[v]
            base = (vx.r0, vx.g0, vx.b0)
            col = tuple(int(base[k] + (tint[k] - base[k]) * w) for k in range(3))
            ed.set_color(v, col)
        n += 1
    return n


def verify(data):
    """Re-read the output and check the rebuilt bounds the same way leveldump does."""
    L = lvl.parse_level(data)
    mat, spec = analyze.material_positions(L), analyze.spectral_positions(L)
    bad = []
    import units
    try:
        starts = {m for m, _ in units.walk_signals(L.drm)}
        base = L.terrain.ptrs["signals"]
        for fi, f in enumerate(L.terrain.faces):
            if f.attr & 0xC0 and f.textoff != 0xFFFF and base + f.textoff not in starts:
                bad.append("trigger face %d points at 0x%x, not at a MultiSignal" % (fi, base + f.textoff))
    except ValueError as e:
        bad.append("signal list: %s" % e)
    for tr in L.terrain.trees:
        st = analyze.walk(tr)
        bad += ["tree %d: %s" % (tr.index, p) for p in st.problems]
        for realm, bc in analyze.check_leaves(L, tr, mat, spec).items():
            if bc.count and bc.box_contains1 != bc.count:   # the original data is up to 1 unit off
                bad.append("tree %d: %d %s leaf boxes do not contain their vertices"
                           % (tr.index, bc.count - bc.box_contains1, realm))
        sp, er = analyze.check_nodes(L, st, mat, spec)
        for realm, bc in sp.items():
            if bc.max_overshoot > 3:
                bad.append("tree %d: %s node sphere misses a vertex by %.1f" % (tr.index, realm, bc.max_overshoot))
        if any(v > 26.5 for v in er.max_diff.values()):
            bad.append("tree %d: error field off by %.1f" % (tr.index, max(er.max_diff.values())))
    return L, bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--disc", help="CD image containing BIGFILE.DAT")
    src.add_argument("--drm", help="level .drm file")
    ap.add_argument("unit", nargs="?", help="unit name (with --disc)")
    ap.add_argument("--material-obj", help="edited material-realm OBJ")
    ap.add_argument("--spectral-obj", help="edited spectral-realm OBJ")
    ap.add_argument("--map", help="map for --material-obj (default <obj export>.map.json next to it)")
    ap.add_argument("--spectral-map", help="map for --spectral-obj")
    ap.add_argument("--no-colors", action="store_true", help="ignore vertex colours in the OBJ")
    ap.add_argument("--crm", help="the unit's .crm, for new texture images when reading a --drm file")
    ap.add_argument("--intros-obj", help="edited <unit>_intros.obj (move, turn, duplicate, delete intros)")
    ap.add_argument("--extra-objects", default="", metavar="TYPE:UNIT,...",
                    help="also load these object types (copied from UNIT), e.g. a hunter's weapon fthrow:city4")
    ap.add_argument("--intro-templates", default="", metavar="UNITS",
                    help="comma-separated units to copy new intro types from (with --disc), e.g. city9")
    ap.add_argument("--hill", nargs=4, type=float, metavar=("X", "Y", "RADIUS", "HEIGHT"),
                    help="test edit: smooth bump of HEIGHT game units around (X, Y)")
    ap.add_argument("--tint", nargs=3, type=int, metavar=("R", "G", "B"), help="colour the --hill towards this")
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
    if not (args.output or args.install):
        ap.error("give -o and/or --install")

    if args.intros_obj:
        import intros
        tmpl = []
        if args.intro_templates:
            if not args.disc:
                ap.error("--intro-templates needs --disc")
            bf = disc.BigFile(args.disc)
            tmpl = [lvl.parse_level(bf.read_level(u.strip())) for u in args.intro_templates.split(",") if u.strip()]
        scale = 0.01
        data, lines, new_types = intros.apply_intros(data, args.intros_obj, scale, tmpl)
        for line in lines:
            print(line)
        args.new_object_types = new_types
    if args.extra_objects:
        import intros
        args.new_object_types = dict(getattr(args, "new_object_types", None) or {})
        for item in args.extra_objects.split(","):
            typ, _, src = item.strip().partition(":")
            data, added = intros.add_object_type(data, typ)
            print("%s %s the unit's object list" % (typ, "added to" if added else "already in"))
            if added:
                args.new_object_types[typ] = src

    if args.material_obj and changes_layout(data, args.material_obj,
                                            args.map or _default_map(args.material_obj, unit, "material")):
        if args.spectral_obj or args.hill:
            ap.error("an OBJ that adds or deletes geometry can't be combined with --spectral-obj or --hill")
        return write_topology(args, data, unit)

    ed = edit.LevelEditor(data)
    if args.material_obj:
        m = args.map or _default_map(args.material_obj, unit, "material")
        moved, recol = apply_obj(ed, args.material_obj, m, "material", not args.no_colors)
        print("material OBJ: %d vertices moved, %d recoloured" % (moved, recol))
    if args.spectral_obj:
        m = args.spectral_map or _default_map(args.spectral_obj, unit, "spectral")
        moved, recol = apply_obj(ed, args.spectral_obj, m, "spectral", not args.no_colors)
        print("spectral OBJ: %d vertices moved, %d recoloured" % (moved, recol))
    if args.hill:
        n = apply_hill(ed, *args.hill, tint=args.tint)
        print("hill: %d vertices" % n)

    out = ed.build()
    for w in ed.warnings[:20]:
        print("warning:", w)
    print("rebuilt: " + ", ".join("%s=%d" % kv for kv in ed.summary.items()))
    print("size %d -> %d bytes" % (len(data), len(out)))
    L, bad = verify(out)
    for b in bad:
        print("CHECK FAILED:", b)
    if bad:
        return 1

    _write(args, unit, out)
    return 0


def changes_layout(data, obj_path, map_path):
    """True if the OBJ adds or deletes vertices, faces or objects."""
    import geometry
    with open(map_path) as f:
        vmap = json.load(f)
    objs = geometry.read_obj_full(obj_path)
    names = {n for n in objs if not n.startswith("portal")}
    if names != set(vmap["objects"]):
        return True
    L = lvl.parse_level(data)
    t = L.terrain
    orig = {}
    for f in t.faces:
        tri = geometry._canon((f.v0, f.v1, f.v2))
        orig[tri] = orig.get(tri, 0) + 1
    seen = {}
    for name in names:
        ids = vmap["objects"][name]["vertices"]
        if len(objs[name]["v"]) != len(ids):
            return True
        for poly in objs[name]["f"]:
            vs = [ids[i] for i in poly]
            for j in range(1, len(vs) - 1):
                tri = geometry._canon((vs[0], vs[j], vs[j + 1]))
                seen[tri] = seen.get(tri, 0) + 1
    degenerate = {k: v for k, v in orig.items() if len(set(k)) < 3}
    if seen != {k: v for k, v in orig.items() if k not in degenerate}:
        return True
    return geometry.obj_has_uv(objs) and textures_changed(L, objs, vmap)


def textures_changed(L, objs, vmap):
    """True if any face's material or UVs in the OBJ differ from the level."""
    import geometry
    import textures
    t = L.terrain
    entries = textures.texture_entries(L)

    def q(x):
        return max(0, min(255, int(round(x * 256))))

    orig = {}
    for f in t.faces:
        tex = textures.face_texture(L, entries, f)
        if tex is None:
            val = ("untextured",)
        else:
            val = (textures.material_name(tex[0], tex[1]),) + tuple(
                (v, uv) for v, uv in sorted(zip((f.v0, f.v1, f.v2), tex[2])))
        key = geometry._canon((f.v0, f.v1, f.v2))
        orig.setdefault(key, []).append(val)
    for name, obj in objs.items():
        if name not in vmap["objects"]:
            continue
        ids = vmap["objects"][name]["vertices"]
        for pi, poly in enumerate(obj["f"]):
            vs = [ids[i] for i in poly]
            uvs = obj["fuv"][pi]
            mname = (obj["fm"][pi] or "").split(".")[0]
            for j in range(1, len(vs) - 1):
                corners = (0, j, j + 1)
                tri = geometry._canon(tuple(vs[c] for c in corners))
                if mname == "untextured" or any(uvs[c] is None for c in corners):
                    val = ("untextured",) if mname == "untextured" else None
                else:
                    val = (mname,) + tuple(sorted((vs[c], (q(uvs[c][0]), q(1.0 - uvs[c][1]))) for c in corners))
                cands = orig.get(tri, [])
                if val is not None and val not in cands:
                    return True
    return False


def write_topology(args, data, unit):
    import geometry
    import bspbuild
    m = args.map or _default_map(args.material_obj, unit, "material")
    crm = None
    if args.crm:
        with open(args.crm, "rb") as f:
            crm = f.read()
    elif args.disc:
        crm = disc.BigFile(args.disc).read_file(disc.level_path(unit).replace(".drm", ".crm"), unit)
    neighbours, watch = (), ()
    if args.disc and _uses_new_images(args.material_obj):
        import newtex
        neighbours, watch = newtex.neighbourhood(disc.BigFile(args.disc), lvl.parse_level(data), unit)
        print("new images: kept clear of %d neighbouring and %d further units; written into %s too" % (
            len(neighbours), len(watch), ", ".join(n for n, _, _ in neighbours) or "no other unit"))
    elif _uses_new_images(args.material_obj):
        print("warning: without --disc, neighbouring units' .crm files can't be updated, and their "
              "textures may be overwritten")
    te = geometry.TopologyEdit(data, crm, neighbours, watch)
    te.apply_obj(args.material_obj, m, use_colors=not args.no_colors)
    out, info = te.build()
    for w in te.warnings[:20]:
        print("warning:", w)
    print("geometry: " + ", ".join("%s=%s" % kv for kv in te.report.items()))
    for s in info["trees"]:
        print("tree %(tree)d: %(faces)d faces -> %(nodes)d nodes, %(leaves)d leaves, depth %(max_depth)d" % s)
    print("size %d -> %d bytes" % (len(data), len(out)))
    _, bad, misses = bspbuild.check(out, 300, "result")
    if bad or misses:
        print("not written")
        return 1
    _write(args, unit, out, te.new_crm)
    for n, c in te.new_neighbour_crms.items():
        _write_crm(args, n, c)
    return 0


def _uses_new_images(obj_path):
    """True if the OBJ's .mtl has a material with an image that isn't a level page."""
    import newtex
    with open(obj_path) as f:
        for line in f:
            if line.startswith("mtllib "):
                mtl = os.path.join(os.path.dirname(os.path.abspath(obj_path)), line[7:].strip())
                if os.path.exists(mtl):
                    return any(v and not k.startswith("tex_") for k, v in newtex.read_mtl(mtl).items())
    return False


def _write_crm(args, unit, crm):
    """A neighbouring unit's patched .crm, next to -o and into --install."""
    paths = []
    if args.output:
        paths.append(os.path.join(os.path.dirname(os.path.abspath(args.output)), unit + ".crm"))
    if args.install:
        paths.append(os.path.join(args.install, *disc.level_path(unit).replace(".drm", ".crm").strip("\\").split("\\")))
    for p in paths:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "wb") as f:
            f.write(crm)
        print("wrote", p, "(neighbour's textures)")


def _write(args, unit, out, crm=None):
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
        if crm is not None:
            cp = os.path.splitext(p)[0] + ".crm"
            with open(cp, "wb") as f:
                f.write(crm)
            print("wrote", cp, "(textures)")
    new_types = getattr(args, "new_object_types", None) or {}
    if new_types:
        if not args.install:
            print("warning: new object types %s need their files in the mods folder; use --install"
                  % ", ".join(new_types))
        else:
            install_object_files(args.disc, new_types, args.install)


def install_object_files(disc_path, new_types, mods_dir):
    r"""Copy \kain2\object\<type>\<type>.* from the template unit's bigfile
    directory into the mods folder (the game looks object files up in the
    current unit's directory, where a type copied from elsewhere is missing)."""
    bf = disc.BigFile(disc_path)
    for typ, src_unit in new_types.items():
        found = 0
        for path in [r"\kain2\object\%s\%s.%s" % (typ, typ, e) for e in disc.HASH_EXTENSIONS] +                 [r"\kain2\sfx\object\%s\%s.%s" % (typ, typ, e) for e in disc.HASH_EXTENSIONS]:
            try:
                blob = bf.read_file(path, src_unit)
            except FileNotFoundError:
                continue
            dst = os.path.join(mods_dir, *path.strip("\\").split("\\"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(blob)
            print("wrote", dst, "(object file from %s)" % src_unit)
            found += 1
        if not found:
            print("warning: no files found for object type %s in %s" % (typ, src_unit))


def _default_map(obj_path, unit, realm):
    cand = [obj_path + ".map.json",
            os.path.join(os.path.dirname(obj_path), "%s_%s.obj.map.json" % (unit, realm))]
    for c in cand:
        if os.path.exists(c):
            return c
    raise SystemExit("no vertex map found for %s; pass --map <export>.obj.map.json" % obj_path)


if __name__ == "__main__":
    sys.exit(main())
