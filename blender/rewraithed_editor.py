"""Rewraithed level editor: a Blender panel around the leveldump tools.

Open a unit from the game, edit its geometry, textures and objects in Blender,
and install the result into the port's mods folder, from one panel in the 3D
view's sidebar (N key, "Rewraithed" tab). See editor/blender/README.md.

The work is done by the command-line tools in editor/leveldump, run with
Blender's own Python, so nothing else needs installing.
"""

bl_info = {
    "name": "Rewraithed level editor",
    "author": "rewraithed",
    "version": (0, 3, 3),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > Rewraithed",
    "description": "Edit Soul Reaver prototype levels for the rewraithed PC port",
    "category": "Import-Export",
}

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

import bpy
import bpy.utils.previews
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
                       StringProperty)
from mathutils import Vector

LOG_NAME = "Rewraithed log"
SCALE = 0.01
INTRO = re.compile(r"^intro_(\d+)_(.+)$")

# what each kind of object looks like: colour, and the library's icon and label
KINDS = [
    ("creature", (0.85, 0.12, 0.08), "GHOST_ENABLED", "Creatures"),
    ("boss", (0.62, 0.18, 0.9), "OUTLINER_OB_ARMATURE", "Bosses"),
    ("object", (0.95, 0.62, 0.12), "OBJECT_DATA", "Objects"),
    ("effect", (0.92, 0.92, 0.92), "LIGHT_POINT", "Effects"),
    ("start", (0.15, 0.9, 0.25), "ARMATURE_DATA", "Raziel's start"),
    ("marker", (0.6, 0.6, 0.6), "EMPTY_SINGLE_ARROW", "Markers"),
]
KIND_COLOUR = {k: c for k, c, _, _ in KINDS}
KIND_ICON = {k: i for k, _, i, _ in KINDS}
# every special ability, Reaver and glyph (the debug menu's three "ALL" entries, DEBUG.c)
ALL_ABILITIES = 0x3F | 0x3FC00 | 0x01FC0000
MARKERS = {"marker", "campath"}       # the unit's own path and camera markers: not in the library


# --- settings ---------------------------------------------------------------

def _addon_key():
    return __package__ or __name__


def _prefs():
    addon = bpy.context.preferences.addons.get(_addon_key())
    return addon.preferences if addon else None


def _game_dir():
    p = _prefs()
    return bpy.path.abspath(p.game_dir) if p and p.game_dir else ""


def _disc_path():
    p = _prefs()
    if p and p.disc_path:
        return bpy.path.abspath(p.disc_path)
    for name in ("KAIN2.img", "KAIN2.bin", "KAIN2.iso"):
        c = os.path.join(_game_dir(), "disc", name)
        if os.path.isfile(c):
            return c
    return ""


def _tools_dir():
    p = _prefs()
    if p and p.tools_dir:
        return bpy.path.abspath(p.tools_dir)
    # running from the repository: editor/blender/ next to editor/leveldump/
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "leveldump")
    return here if os.path.isfile(os.path.join(here, "levelpatch.py")) else ""


def _mods_dir():
    return os.path.join(_game_dir(), "mods")


def _work_dir(*parts):
    return os.path.join(_game_dir(), "editor-work", *parts)


def _setup_problem():
    if not _game_dir() or not os.path.isfile(os.path.join(_game_dir(), "rewraithed.exe")):
        return "Set the game folder (the one with rewraithed.exe) in the add-on's preferences"
    if not _disc_path() or not os.path.isfile(_disc_path()):
        return "Set the disc image (KAIN2.img) in the add-on's preferences"
    if not _tools_dir() or not os.path.isfile(os.path.join(_tools_dir(), "levelpatch.py")):
        return "Set the editor's leveldump folder in the add-on's preferences"
    return None


class RewraithedPreferences(bpy.types.AddonPreferences):
    bl_idname = _addon_key()

    game_dir: StringProperty(name="Game folder", subtype="DIR_PATH",
                             description="The folder with rewraithed.exe (its mods folder is written)")
    disc_path: StringProperty(name="Disc image", subtype="FILE_PATH",
                              description="KAIN2.img; empty: <game folder>/disc/KAIN2.img")
    tools_dir: StringProperty(name="Editor tools", subtype="DIR_PATH",
                              description="editor/leveldump in the rewraithed repository; "
                                          "empty: found next to this add-on")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "game_dir")
        col.prop(self, "disc_path")
        col.prop(self, "tools_dir")
        problem = _setup_problem()
        col.label(text=problem or "Ready", icon="ERROR" if problem else "CHECKMARK")


# --- running the tools --------------------------------------------------------

def _python():
    exe = sys.executable
    if exe and os.path.basename(exe).lower().startswith("python"):
        return exe
    # older Blender builds: sys.executable is blender itself
    for root, _, files in os.walk(os.path.dirname(bpy.app.binary_path)):
        for f in files:
            if f.lower() in ("python.exe", "python3.exe", "python3"):
                return os.path.join(root, f)
    return "python"


def _log(text, clear=False):
    t = bpy.data.texts.get(LOG_NAME) or bpy.data.texts.new(LOG_NAME)
    if clear:
        t.clear()
    t.write(text if text.endswith("\n") else text + "\n")
    print(text)


def _run(tool, *args):
    """Run editor/leveldump/<tool>.py; returns (exit code, output)."""
    cmd = [_python(), os.path.join(_tools_dir(), tool + ".py")] + [str(a) for a in args]
    _log("> " + " ".join(os.path.basename(c) if i < 2 else c for i, c in enumerate(cmd)))
    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
    r = subprocess.run(cmd, cwd=_tools_dir(), capture_output=True, text=True, creationflags=flags)
    out = (r.stdout or "") + (r.stderr or "")
    _log(out.rstrip() or "(no output)")
    if r.returncode:
        _log("exit code %d" % r.returncode)
    return r.returncode, out


def _problems(out):
    """Lines of a tool's output that need the user's attention."""
    keys = ("CHECK FAILED", "not written", "OVER", "warning", "Traceback", "Error", "error:")
    return [ln.strip() for ln in out.splitlines() if any(k in ln for k in keys)]


# --- the scene ----------------------------------------------------------------

def _col_name(unit, part):
    return "%s %s" % (unit, part)


def _collection(scene, name, parent=None):
    c = bpy.data.collections.get(name)
    if c is None:
        c = bpy.data.collections.new(name)
    parent = parent or scene.collection
    if c.name not in [x.name for x in parent.children]:
        parent.children.link(c)
    return c


def _layer_collection(layer, name):
    def walk(lc):
        if lc.collection.name == name:
            return lc
        for ch in lc.children:
            r = walk(ch)
            if r:
                return r
        return None
    return walk(layer.layer_collection)


def _remove_collection(name):
    c = bpy.data.collections.get(name)
    if c is None:
        return
    for ch_name in [ch.name for ch in c.children]:
        _remove_collection(ch_name)
    for o in list(c.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.data.collections.remove(c)


def _clear_editor_scene():
    # by name: removing a unit's collection removes its sub-collections too, and a
    # Python reference to one of those would then be dead
    for name in [c.name for c in bpy.data.collections if c.get("rewraithed")]:
        _remove_collection(name)
    for _ in range(3):
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)


def _import_obj(path, collection, context, keep_names=False):
    lc = _layer_collection(context.view_layer, collection.name)
    context.view_layer.active_layer_collection = lc
    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=path, forward_axis="Y", up_axis="Z")
    new = [o for o in bpy.data.objects if o not in before]
    for o in new:
        for c in list(o.users_collection):
            if c != collection:
                c.objects.unlink(o)
        if o.name not in collection.objects:
            collection.objects.link(o)
        if keep_names:
            # the material and spectral files use the same object names, so
            # Blender gives the second set a ".001": remember the file's name
            o["rewr_name"] = re.sub(r"\.\d{3}$", "", o.name)
    return new


def _fingerprint(objects):
    h = hashlib.sha1()
    for o in sorted(objects, key=lambda o: o.name):
        h.update(o.name.encode())
        if o.type == "MESH":
            me = o.data
            h.update(str(len(me.vertices)).encode() + str(len(me.polygons)).encode())
            for v in me.vertices:
                co = o.matrix_world @ v.co
                h.update(("%.4f %.4f %.4f;" % tuple(co)).encode())
            for a in me.color_attributes:
                for d in a.data:
                    h.update(("%.3f %.3f %.3f;" % tuple(d.color[:3])).encode())
    return h.hexdigest()


def _export_obj(path, objects, context, materials, collection=None):
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    lc = _layer_collection(context.view_layer, collection.name) if collection else None
    was_hidden = lc.hide_viewport if lc else False
    if lc:
        lc.hide_viewport = False
    for o in context.view_layer.objects:
        o.select_set(False)
    hidden = []
    for o in objects:
        if o.hide_get():
            o.hide_set(False)
            hidden.append(o)
        o.select_set(True)
    if objects:
        context.view_layer.objects.active = objects[0]
    # write the names the tools know (see _import_obj)
    renamed = []
    for o in objects:
        want = o.get("rewr_name")
        if want and o.name != want:
            other = bpy.data.objects.get(want)
            if other is not None:
                old = other.name
                other.name = want + "~"
                renamed.append((other, old))
            renamed.append((o, o.name))
            o.name = want
    bpy.ops.wm.obj_export(filepath=path, forward_axis="Y", up_axis="Z", export_selected_objects=True,
                          apply_modifiers=True, export_colors=True, export_uv=True, export_normals=False,
                          export_materials=materials, path_mode="ABSOLUTE",
                          export_triangulated_mesh=True)
    for o, old in renamed:
        o.name = old + "~~"
    for o, old in renamed:
        o.name = old
    for o in hidden:
        o.hide_set(True)
    if lc:
        lc.hide_viewport = was_hidden


def _game_look(material):
    """Draw a material the way the game does: texture times vertex colour
    (128 = unchanged, so times 2), unlit, sharp texels, palette colour 0 clear.
    Only the node setup changes; the material's name, which the tools read, stays."""
    material.use_nodes = True
    nt = material.node_tree
    img = next((n.image for n in nt.nodes if n.type == "TEX_IMAGE" and n.image), None)
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (900, 0)
    vcol = nt.nodes.new("ShaderNodeVertexColor")      # the mesh's active colour attribute
    vcol.location = (-300, -250)
    lit = nt.nodes.new("ShaderNodeMix")
    lit.data_type = "RGBA"
    lit.blend_type = "MULTIPLY"
    lit.inputs[0].default_value = 1.0
    lit.location = (100, 0)
    twice = nt.nodes.new("ShaderNodeMix")
    twice.data_type = "RGBA"
    twice.blend_type = "MULTIPLY"
    twice.inputs[0].default_value = 1.0
    twice.inputs[7].default_value = (2.0, 2.0, 2.0, 1.0)
    twice.location = (300, 0)
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.location = (500, 0)
    nt.links.new(lit.outputs[2], twice.inputs[6])
    nt.links.new(twice.outputs[2], emit.inputs["Color"])
    nt.links.new(vcol.outputs["Color"], lit.inputs[7])
    if img is not None:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.interpolation = "Closest"
        tex.location = (-300, 100)
        nt.links.new(tex.outputs["Color"], lit.inputs[6])
        clear = nt.nodes.new("ShaderNodeBsdfTransparent")
        clear.location = (500, 150)
        mix = nt.nodes.new("ShaderNodeMixShader")
        mix.location = (700, 0)
        nt.links.new(tex.outputs["Alpha"], mix.inputs[0])
        nt.links.new(clear.outputs[0], mix.inputs[1])
        nt.links.new(emit.outputs[0], mix.inputs[2])
        nt.links.new(mix.outputs[0], out.inputs["Surface"])
        if hasattr(material, "surface_render_method"):
            material.surface_render_method = "DITHERED"
        else:
            material.blend_method = "CLIP"
    else:
        lit.inputs[6].default_value = (1.0, 1.0, 1.0, 1.0)
        nt.links.new(emit.outputs[0], out.inputs["Surface"])
    material.use_backface_culling = False


def _show_textured(context, objects):
    """Material preview in every 3D view, colours as they are, and the view on the unit."""
    scene = context.scene
    try:
        scene.view_settings.view_transform = "Standard"   # the default (AgX/Filmic) washes colours out
        scene.view_settings.look = "None"
    except TypeError:
        pass
    screen = context.screen
    if screen is None:
        return
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        space = area.spaces.active
        space.shading.type = "MATERIAL"
        space.clip_end = max(space.clip_end, 2000.0)      # a unit is a few hundred metres across
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        if region is not None and objects:
            for o in context.view_layer.objects:
                o.select_set(False)
            for o in objects:
                if o.visible_get():
                    o.select_set(True)
            with context.temp_override(area=area, region=region):
                bpy.ops.view3d.view_selected()
            for o in objects:
                o.select_set(False)


def _set_realm(scene, realm):
    unit = scene.rewr_open_unit
    for part, show in (("material", realm == "MATERIAL"), ("spectral", realm == "SPECTRAL")):
        lc = _layer_collection(bpy.context.view_layer, _col_name(unit, part))
        if lc:
            lc.hide_viewport = not show
            lc.collection.hide_render = not show


def _realm_update(self, context):
    _set_realm(context.scene, self.rewr_realm)


# --- the object library ---------------------------------------------------------

_CATALOG = {"path": None, "mtime": None, "data": {"types": {}, "units": []}}


def _catalog_path():
    return _work_dir("catalog.json")


def _load_catalog(rebuild=False):
    """Every object type in the game (objcatalog.py), built once into the work
    folder; fills the library list."""
    path = _catalog_path()
    if rebuild or not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _run("objcatalog", "--disc", _disc_path(), "--mods", _mods_dir(), "--out", path)
    if not _read_catalog():
        return _CATALOG["data"]
    _fill_library(bpy.context.scene)
    return _CATALOG["data"]


def _read_catalog():
    path = _catalog_path()
    if not os.path.isfile(path):
        return False
    mtime = os.path.getmtime(path)
    if _CATALOG["path"] != path or _CATALOG["mtime"] != mtime:
        with open(path) as f:
            _CATALOG.update(path=path, mtime=mtime, data=json.load(f))
    return True


def _types():
    if _CATALOG["path"] is None and _game_dir():
        try:
            _read_catalog()            # e.g. after reopening a saved .blend
        except (OSError, ValueError):
            pass
    return _CATALOG["data"].get("types", {})


def _kind(typ):
    if typ in MARKERS:
        return "marker"
    e = _types().get(typ)
    return e["kind"] if e else "object"


def _fill_library(scene):
    order = {k: i for i, (k, _, _, _) in enumerate(KINDS)}
    lib = scene.rewr_library
    lib.clear()
    for typ, e in sorted(_types().items(), key=lambda kv: (order.get(kv[1]["kind"], 9), kv[0])):
        it = lib.add()
        it.name = typ
        it.kind = e["kind"]
        it.count = e["count"]
    scene.rewr_library_index = min(scene.rewr_library_index, max(len(lib) - 1, 0))
    units = scene.rewr_units
    units.clear()
    for u in sorted(_CATALOG["data"].get("units", []), key=_natural):
        units.add().name = u


def _natural(name):
    """city2 before city10"""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


# --- thumbnails -------------------------------------------------------------------

_PREVIEWS = {"coll": None}
THUMB = 128


def _previews():
    if _PREVIEWS["coll"] is None:
        _PREVIEWS["coll"] = bpy.utils.previews.new()
    return _PREVIEWS["coll"]


def _render_thumb(verts, faces, colour, size=THUMB):
    """The model seen from the front right and a little above, flat shaded:
    RGBA floats, bottom row first (as Blender's previews want them)."""
    import numpy as np
    V = np.asarray(verts, dtype=np.float64)
    F = np.asarray(faces, dtype=np.int64)
    a, e = math.radians(-35), math.radians(18)
    x = V[:, 0] * math.cos(a) - V[:, 1] * math.sin(a)
    y = V[:, 0] * math.sin(a) + V[:, 1] * math.cos(a)
    sy = V[:, 2] * math.cos(e) + y * math.sin(e)
    depth = y * math.cos(e) - V[:, 2] * math.sin(e)          # the camera is at -Y: smaller is nearer
    span = max(x.max() - x.min(), sy.max() - sy.min(), 1e-6)
    k = (size * 0.9) / span
    px = (x - (x.max() + x.min()) / 2) * k + size / 2
    py = (sy - (sy.max() + sy.min()) / 2) * k + size / 2
    P = np.stack([px, py, depth], axis=1)
    img = np.zeros((size, size, 4))
    zbuf = np.full((size, size), np.inf)
    light = np.array([0.4, -0.75, 0.55])
    light /= np.linalg.norm(light)
    view = np.stack([x, y, V[:, 2]], axis=1)
    for f in F:
        p0, p1, p2 = P[f[0]], P[f[1]], P[f[2]]
        n = np.cross(view[f[1]] - view[f[0]], view[f[2]] - view[f[0]])
        ln = np.linalg.norm(n)
        d = (p1[1] - p2[1]) * (p0[0] - p2[0]) + (p2[0] - p1[0]) * (p0[1] - p2[1])
        if ln == 0 or abs(d) < 1e-9:
            continue
        shade = 0.35 + 0.65 * abs(float(np.dot(n / ln, light)))
        x0 = int(max(0, math.floor(min(p0[0], p1[0], p2[0]))))
        x1 = int(min(size - 1, math.ceil(max(p0[0], p1[0], p2[0]))))
        y0 = int(max(0, math.floor(min(p0[1], p1[1], p2[1]))))
        y1 = int(min(size - 1, math.ceil(max(p0[1], p1[1], p2[1]))))
        if x1 < x0 or y1 < y0:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        w0 = ((p1[1] - p2[1]) * (gx - p2[0]) + (p2[0] - p1[0]) * (gy - p2[1])) / d
        w1 = ((p2[1] - p0[1]) * (gx - p2[0]) + (p0[0] - p2[0]) * (gy - p2[1])) / d
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        z = w0 * p0[2] + w1 * p1[2] + w2 * p2[2]
        sub = zbuf[y0:y1 + 1, x0:x1 + 1]
        win = inside & (z < sub)
        sub[win] = z[win]
        img[y0:y1 + 1, x0:x1 + 1][win] = (colour[0] * shade, colour[1] * shade, colour[2] * shade, 1.0)
    return img.ravel()


def _model_preview(typ):
    """icon_id of the type's thumbnail, or 0 if it has no model to show."""
    key = "model " + typ
    pc = _previews()
    if key in pc:
        return pc[key].icon_id
    m = (_types().get(typ) or {}).get("model")
    if not m or len(m["faces"]) < 2:
        return 0
    try:
        pixels = _render_thumb(m["verts"], m["faces"], _type_colour(typ))
    except Exception as ex:                # never break the panel over a thumbnail
        print("rewraithed: no thumbnail for %s: %s" % (typ, ex))
        return 0
    pv = pc.new(key)
    pv.image_size = (THUMB, THUMB)
    pv.image_pixels_float = pixels
    return pv.icon_id


def _play_icon():
    """A green play triangle for the Play button."""
    pc = _previews()
    if "play" not in pc:
        n = 32
        px = []
        for y in range(n):
            for x in range(n):
                inside = 7 <= x <= 26 and abs(y - 15.5) <= (26 - x) * 0.6
                px += (0.2, 0.85, 0.3, 1.0) if inside else (0.0, 0.0, 0.0, 0.0)
        pv = pc.new("play")
        pv.image_size = (n, n)
        pv.image_pixels_float = px
    return pc["play"].icon_id


def _type_colour(typ):
    """Raziel green, effects white, markers grey; every other type its own colour."""
    kind = _kind(typ)
    if kind in ("start", "effect", "marker"):
        return KIND_COLOUR[kind]
    import colorsys
    import zlib
    h = zlib.crc32(typ.encode())
    hue = (h % 1000) / 1000.0
    if 0.22 < hue < 0.42:          # keep clear of Raziel's green
        hue = 0.42 + (hue - 0.22) * 1.5
    return colorsys.hsv_to_rgb(hue % 1.0, 0.55 + (h >> 10) % 30 / 100.0, 0.95)


def _type_material(typ):
    kind = _kind(typ)
    name = "rewr %s" % (kind if kind in ("start", "effect", "marker") else typ)
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        col = tuple(_type_colour(typ)) + (1.0,)
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = col
            bsdf.inputs["Roughness"].default_value = 0.8
            # a little glow, so dark corners don't hide them
            if "Emission Color" in bsdf.inputs:
                bsdf.inputs["Emission Color"].default_value = col
                bsdf.inputs["Emission Strength"].default_value = 0.25
        m.diffuse_color = col
    return m


def _type_mesh(typ):
    """The type's model (its first model in its rest pose), with a pointer on the
    ground in front of it; a small diamond for things with nothing to see."""
    name = "rewr model %s" % typ
    me = bpy.data.meshes.get(name)
    if me is not None:
        return me
    kind = _kind(typ)
    e = _types().get(typ) or {}
    m = e.get("model")
    verts, faces = [], []
    if m and len(m["faces"]) >= 2:
        verts = [(x * SCALE, y * SCALE, z * SCALE) for x, y, z in m["verts"]]
        faces = [tuple(f) for f in m["faces"]]
        front = min(0.0, min(v[1] for v in verts))
        size = 1.0
    else:
        r, h = (0.12, 0.12) if kind == "marker" else (0.25, 0.3)
        verts = [(r, 0, h), (-r, 0, h), (0, r, h), (0, -r, h), (0, 0, h + r), (0, 0, h - r)]
        faces = [(0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4), (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5)]
        front = -r
        size = 0.5 if kind == "marker" else 1.0
    # models face -Y: the pointer shows where the object looks
    b = len(verts)
    verts += [(0, front - 0.7 * size, 0.02), (-0.25 * size, front - 0.15 * size, 0.02),
              (0.25 * size, front - 0.15 * size, 0.02)]
    faces.append((b, b + 2, b + 1))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    me.materials.append(_type_material(typ))
    return me


def _make_intro(collection, typ, uid, pos, yaw):
    o = bpy.data.objects.new(typ, _type_mesh(typ))
    collection.objects.link(o)
    o.location = pos
    o.rotation_euler = (0.0, 0.0, yaw)
    o["rewr_type"] = typ
    o["rewr_uid"] = uid
    o["rewr_pos"] = tuple(pos)
    o.lock_rotation = (True, True, False)       # the game keeps their tilt; turn them around Z
    o.lock_scale = (True, True, True)
    o.show_name = False          # _draw_labels draws a clearer one
    o.color = tuple(_type_colour(typ)) + (1.0,)
    return o


def _read_intros_obj(path):
    """[(UniqueID, type, position, yaw)] from leveldump's intros OBJ: each arrow's
    first vertex is the position, its second the tip (rotation 0 points along +Y)."""
    objs, cur = [], None
    with open(path) as f:
        for line in f:
            if line.startswith("o "):
                cur = [line[2:].strip(), []]
                objs.append(cur)
            elif line.startswith("v ") and cur is not None and len(cur[1]) < 2:
                cur[1].append(tuple(float(v) for v in line.split()[1:4]))
    out = []
    for name, v in objs:
        m = INTRO.match(name)
        if m and len(v) == 2:
            dx, dy = v[1][0] - v[0][0], v[1][1] - v[0][1]
            # types are matched without case ("Hunter" in one unit, "hunter" in another)
            out.append((int(m.group(1)), m.group(2).lower(), v[0], math.atan2(-dx, dy)))
    return out


def _write_intros_obj(path, objects):
    """The objects as levelpatch.py reads intros: "intro_<UniqueID>_<type>" for the
    unit's own (a copy of one gets a ".001": a new intro copied from it, which
    keeps the original's settings), "intro_new_<type>" for one from the library."""
    keep = {}
    for o in objects:
        uid = int(o.get("rewr_uid", 0))
        if uid:
            orig = Vector(o.get("rewr_pos", (0, 0, 0)))
            d = (o.matrix_world.translation - orig).length
            if uid not in keep or d < keep[uid][0]:
                keep[uid] = (d, o)
    dups = {}
    lines = ["# intros written by the rewraithed Blender add-on"]
    for o in objects:
        typ = o["rewr_type"]
        uid = int(o.get("rewr_uid", 0))
        if uid and keep[uid][1] is o:
            name = "intro_%d_%s" % (uid, typ)
        elif uid:
            dups[uid] = dups.get(uid, 0) + 1
            name = "intro_%d_%s.%03d" % (uid, typ, dups[uid])
        else:
            name = "intro_new_%s" % typ
        p = o.matrix_world.translation
        a = o.matrix_world.to_euler("XYZ").z
        tip = p + Vector((-math.sin(a), math.cos(a), 0.0)) * 0.6
        lines += ["o " + name, "v %.4f %.4f %.4f" % tuple(p), "v %.4f %.4f %.4f" % tuple(tip)]
    with open(path, "w", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def _drop_to_floor(context, o):
    """Put the object on the visible terrain under it; False if there is none."""
    origin = o.matrix_world.translation + Vector((0, 0, 0.5))
    best = None
    for t in context.view_layer.objects:
        if not t.get("rewr_terrain") or t.type != "MESH" or not t.visible_get():
            continue
        inv = t.matrix_world.inverted()
        lo = inv @ origin
        ld = (inv.to_3x3() @ Vector((0, 0, -1))).normalized()
        hit, loc, _, _ = t.ray_cast(lo, ld, distance=500.0)
        if hit:
            z = (t.matrix_world @ loc).z
            if best is None or z > best:
                best = z
    if best is None:
        return False
    o.location.z = best
    return True


def _apply_snap(context):
    s = context.scene
    ts = s.tool_settings
    if s.rewr_snap == "OFF":
        ts.use_snap = False
    elif s.rewr_snap == "GRID":
        ts.use_snap = True
        ts.snap_elements = {"INCREMENT"}
        ts.use_snap_grid_absolute = True
        ts.use_snap_translate = ts.use_snap_rotate = True
        ts.snap_angle_increment_3d = math.radians(15)
    else:
        ts.use_snap = True
        ts.snap_elements = {"FACE_PROJECT"}
        ts.use_snap_align_rotation = False
        ts.use_snap_translate = True
    screen = context.screen
    for area in (screen.areas if screen else []):
        if area.type == "VIEW_3D":
            ov = area.spaces.active.overlay
            ov.grid_scale = s.rewr_grid
            ov.grid_subdivisions = 1


def _snap_update(self, context):
    _apply_snap(context)


# --- labels -----------------------------------------------------------------------

LABEL_NEAR, LABEL_FAR = 30.0, 60.0        # metres from the view: full, then fading out
_LABELS = {"handle": None}


def _redraw_views(self=None, context=None):
    for w in bpy.context.window_manager.windows:
        for a in w.screen.areas:
            if a.type == "VIEW_3D":
                a.tag_redraw()


def _draw_labels():
    """A dark box with the type's name above each object, framed in its colour
    (orange when selected). Far ones fade out; markers only show when selected."""
    import blf
    import gpu
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    from gpu_extras.batch import batch_for_shader
    ctx = bpy.context
    s = ctx.scene
    if s is None or not s.rewr_open_unit or not s.rewr_labels:
        return
    col = bpy.data.collections.get(_col_name(s.rewr_open_unit, "objects"))
    region, rv3d = ctx.region, ctx.region_data
    if col is None or region is None or rv3d is None:
        return
    eye = rv3d.view_matrix.inverted().translation
    ui = ctx.preferences.system.ui_scale
    font = 0
    blf.size(font, 12 * ui)
    _, text_h = blf.dimensions(font, "Mg")
    pad = 4 * ui
    items = []
    for o in col.objects:
        typ = o.get("rewr_type")
        if not typ or not o.visible_get():
            continue
        sel = o.select_get()
        if _kind(typ) == "marker" and not sel:
            continue
        top = max(v[2] for v in o.bound_box) + 0.2
        at = o.matrix_world @ Vector((0.0, 0.0, top))
        dist = (at - eye).length if rv3d.is_perspective else 0.0
        if dist > LABEL_FAR and not sel:
            continue
        p = location_3d_to_region_2d(region, rv3d, at)
        if p is None:
            continue
        alpha = 1.0 if sel or dist <= LABEL_NEAR else 1.0 - (dist - LABEL_NEAR) / (LABEL_FAR - LABEL_NEAR)
        items.append((sel, -dist, typ, p, alpha))
    if not items:
        return
    items.sort(key=lambda t: (t[0], t[1]))        # far ones first, selected on top
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    for sel, _, typ, p, alpha in items:
        w, _ = blf.dimensions(font, typ)
        x0, x1 = p.x - w / 2 - pad, p.x + w / 2 + pad
        y0, y1 = p.y, p.y + text_h + 2 * pad
        quad = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        shader.bind()
        shader.uniform_float("color", (0.04, 0.04, 0.05, 0.8 * alpha))
        batch_for_shader(shader, "TRIS", {"pos": quad}, indices=((0, 1, 2), (2, 3, 0))).draw(shader)
        edge = (1.0, 0.55, 0.1) if sel else tuple(_type_colour(typ))
        shader.uniform_float("color", edge + (alpha,))
        batch_for_shader(shader, "LINE_STRIP", {"pos": quad + [quad[0]]}).draw(shader)
        blf.position(font, x0 + pad, y0 + pad + text_h * 0.15, 0)
        blf.color(font, 1.0, 1.0, 1.0, alpha)
        blf.draw(font, typ)
    gpu.state.blend_set("NONE")


def open_unit(context, unit):
    """Export `unit` (as it is now in the game: the mods folder's copy if
    there) and load it into the scene. Returns (ok, message)."""
    scene = context.scene
    unit = unit.strip().lower()
    out_dir = _work_dir(unit)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    code, out = _run("leveldump", "--disc", _disc_path(), "--mods", _mods_dir(), unit, "--obj", out_dir)
    if code or not os.path.isfile(os.path.join(out_dir, unit + "_material.obj")):
        return False, "Could not open %s (see the %s text)" % (unit, LOG_NAME)

    _load_catalog()
    _clear_editor_scene()
    root = _collection(scene, "Rewraithed: " + unit)
    root["rewraithed"] = True
    cols = {}
    for part in ("material", "spectral", "objects"):
        c = _collection(scene, _col_name(unit, part), root)
        c["rewraithed"] = True
        cols[part] = c
    terrain = _import_obj(os.path.join(out_dir, unit + "_material.obj"), cols["material"], context, True)
    terrain += _import_obj(os.path.join(out_dir, unit + "_spectral.obj"), cols["spectral"], context, True)
    terrain_mats = set()
    for o in terrain:
        o["rewr_terrain"] = True
        terrain_mats.update(s.material for s in o.material_slots if s.material)
    for m in terrain_mats:
        _game_look(m)

    intros = []
    path = os.path.join(out_dir, unit + "_intros.obj")
    if os.path.isfile(path):
        intros = _read_intros_obj(path)
    for uid, typ, pos, yaw in intros:
        _make_intro(cols["objects"], typ, uid, pos, yaw)

    scene.rewr_open_unit = unit
    scene["rewr_unit_types"] = ",".join(sorted({typ for _, typ, _, _ in intros}))
    scene["rewr_fp_material"] = _fingerprint(cols["material"].objects)
    scene["rewr_fp_spectral"] = _fingerprint(cols["spectral"].objects)
    _set_realm(scene, scene.rewr_realm)
    _apply_snap(context)
    _show_textured(context, list(cols["material"].objects))
    in_mods = os.path.isfile(os.path.join(_mods_dir(), *_level_path(unit).split("\\")))
    return True, "Opened %s (%s)" % (unit, "your edited version" if in_mods else "as on the disc")


def _level_path(unit, ext="drm"):
    area = re.match(r"[a-z]+", unit).group(0)
    return "kain2\\area\\%s\\bin\\%s.%s" % (area, unit, ext)


# --- operators ----------------------------------------------------------------

class _ToolOperator:
    def _ready(self):
        problem = _setup_problem()
        if problem:
            self.report({"ERROR"}, problem)
            return False
        return True


class REWR_OT_open_unit(_ToolOperator, bpy.types.Operator):
    """Load a unit into Blender: its edited version from the mods folder if there is one,
    else the disc's. Replaces what the editor loaded before (unsaved Blender edits are lost)"""
    bl_idname = "rewraithed.open_unit"
    bl_label = "Open unit"
    bl_options = {"UNDO"}        # Ctrl+Z must not step back past unrecorded changes

    def execute(self, context):
        if not self._ready():
            return {"CANCELLED"}
        unit = context.scene.rewr_unit.strip().lower()
        if not re.fullmatch(r"[a-z]+[0-9]+", unit):
            self.report({"ERROR"}, "A unit name is letters then a number, e.g. city9")
            return {"CANCELLED"}
        _log("Opening %s" % unit, clear=True)
        ok, msg = open_unit(context, unit)
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"} if ok else {"CANCELLED"}


class REWR_OT_add_object(_ToolOperator, bpy.types.Operator):
    """Put the object picked in the library at the 3D cursor (Shift+right click on the
    ground places the cursor), standing on the floor under it"""
    bl_idname = "rewraithed.add_object"
    bl_label = "Add at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    typ: StringProperty(name="Type", options={"SKIP_SAVE"})

    def execute(self, context):
        s = context.scene
        col = bpy.data.collections.get(_col_name(s.rewr_open_unit, "objects")) if s.rewr_open_unit else None
        if col is None:
            self.report({"ERROR"}, "Open a unit first")
            return {"CANCELLED"}
        typ = self.typ
        if not typ:
            if not 0 <= s.rewr_library_index < len(s.rewr_library):
                self.report({"ERROR"}, "Pick an object in the library first")
                return {"CANCELLED"}
            typ = s.rewr_library[s.rewr_library_index].name
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        o = _make_intro(col, typ, 0, s.cursor.location.copy(), 0.0)
        context.view_layer.update()
        on_floor = _drop_to_floor(context, o)
        for x in context.view_layer.objects:
            x.select_set(False)
        o.select_set(True)
        context.view_layer.objects.active = o
        self.report({"INFO"}, "Added %s%s; G moves it, R turns it" % (
            typ, "" if on_floor else " (no floor under the cursor)"))
        return {"FINISHED"}


_LAST_PICK = {"index": -1, "time": 0.0}
DOUBLE_CLICK = 0.45      # seconds


class REWR_OT_pick_type(bpy.types.Operator):
    """Pick an object type; double-click to add it at the 3D cursor"""
    bl_idname = "rewraithed.pick_type"
    bl_label = "Pick object type"
    bl_options = {"INTERNAL", "UNDO"}     # a double-click adds an object: one undo step

    library_index: IntProperty(options={"SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        lib = context.scene.rewr_library
        if not 0 <= properties.library_index < len(lib):
            return cls.__doc__
        typ = lib[properties.library_index].name
        e = _types().get(typ, {})
        units = sorted(e.get("units", {}).items(), key=lambda kv: -kv[1])
        where = ", ".join(u for u, _ in units[:5])
        if len(units) > 5:
            where += " and %d more units" % (len(units) - 5)
        return "%s: %d in the game, in %s.\nDouble-click to add one at the 3D cursor, or right-click" % (
            typ, e.get("count", 0), where)

    def execute(self, context):
        s = context.scene
        i = self.library_index
        now = time.monotonic()
        double = _LAST_PICK["index"] == i and now - _LAST_PICK["time"] < DOUBLE_CLICK
        s.rewr_library_index = i
        if double:
            _LAST_PICK.update(index=-1, time=0.0)     # a third click starts over
            if 0 <= i < len(s.rewr_library):
                bpy.ops.rewraithed.add_object("EXEC_DEFAULT", typ=s.rewr_library[i].name)
        else:
            _LAST_PICK.update(index=i, time=now)
        return {"FINISHED"}


def _list_context_menu(self, context):
    """Right-click on a library row: add that type."""
    op = getattr(context, "button_operator", None)
    if op is None or "library_index" not in op.bl_rna.properties:
        return
    lib = context.scene.rewr_library
    if not 0 <= op.library_index < len(lib):
        return
    typ = lib[op.library_index].name
    lay = self.layout
    lay.operator("rewraithed.add_object", text="Add %s at the 3D cursor" % typ, icon="ADD").typ = typ
    lay.separator()


class REWR_OT_drop_to_floor(bpy.types.Operator):
    """Stand the selected objects on the terrain under them (the realm you see)"""
    bl_idname = "rewraithed.drop_to_floor"
    bl_label = "Drop to floor"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sel = [o for o in context.selected_objects if o.get("rewr_type")]
        if not sel:
            self.report({"ERROR"}, "Select one or more objects of the unit first")
            return {"CANCELLED"}
        missed = [o.name for o in sel if not _drop_to_floor(context, o)]
        if missed:
            self.report({"WARNING"}, "No floor under: " + ", ".join(missed))
        return {"FINISHED"}


class REWR_OT_refresh_library(_ToolOperator, bpy.types.Operator):
    """Read the list of every object type in the game again (after installing new units)"""
    bl_idname = "rewraithed.refresh_library"
    bl_label = "Refresh library"
    bl_options = {"UNDO"}        # Ctrl+Z must not step back past unrecorded changes

    def execute(self, context):
        if not self._ready():
            return {"CANCELLED"}
        _log("Reading every object type in the game", clear=True)
        _load_catalog(rebuild=True)
        self.report({"INFO"}, "%d object types" % len(_types()))
        return {"FINISHED"}


class REWR_OT_install(_ToolOperator, bpy.types.Operator):
    """Write the edited unit into the game's mods folder, check it, and reload it here"""
    bl_idname = "rewraithed.install"
    bl_label = "Install into the game"
    bl_options = {"UNDO"}        # Ctrl+Z must not step back past unrecorded changes

    def execute(self, context):
        if not self._ready():
            return {"CANCELLED"}
        scene = context.scene
        unit = scene.rewr_open_unit
        if not unit:
            self.report({"ERROR"}, "Open a unit first")
            return {"CANCELLED"}
        work = _work_dir(unit)
        mat_col = bpy.data.collections.get(_col_name(unit, "material"))
        spec_col = bpy.data.collections.get(_col_name(unit, "spectral"))
        obj_col = bpy.data.collections.get(_col_name(unit, "objects"))
        if mat_col is None or obj_col is None:
            self.report({"ERROR"}, "The unit's collections are missing; open the unit again")
            return {"CANCELLED"}
        _log("Installing %s" % unit, clear=True)
        _load_catalog()

        edit_dir = os.path.join(work, "edited")
        os.makedirs(edit_dir, exist_ok=True)
        mat_obj = os.path.join(edit_dir, unit + "_material.obj")
        intros_obj = os.path.join(edit_dir, unit + "_intros.obj")
        _export_obj(mat_obj, [o for o in mat_col.all_objects if o.type == "MESH"], context, True, mat_col)
        placed = [o for o in obj_col.all_objects if o.get("rewr_type")]
        if not placed:
            self.report({"ERROR"}, "The unit has no objects left; keep at least one")
            return {"CANCELLED"}
        _write_intros_obj(intros_obj, placed)
        # an object type new to the unit is copied from a unit that has one
        have = set(scene.get("rewr_unit_types", "").split(","))
        templates = []
        for o in placed:
            e = _types().get(o["rewr_type"])
            if not o.get("rewr_uid") and o["rewr_type"] not in have and e and e.get("template") \
                    and e["template"] != unit and e["template"] not in templates:
                templates.append(e["template"])

        spectral_changed = spec_col is not None and \
            _fingerprint(spec_col.objects) != scene.get("rewr_fp_spectral")
        material_changed = _fingerprint(mat_col.objects) != scene.get("rewr_fp_material")

        args = ["--disc", _disc_path(), unit, "--from-mods", "--install", _mods_dir(),
                "--material-obj", mat_obj, "--map", os.path.join(work, unit + "_material.obj.map.json"),
                "--intros-obj", intros_obj]
        if templates:
            args += ["--intro-templates", ",".join(templates)]
        code, out = _run("levelpatch", *args)
        problems = _problems(out)
        if code or "not written" in out or "CHECK FAILED" in out:
            self.report({"ERROR"}, "Not installed: %s (see the %s text)" % (
                problems[0] if problems else "the tool failed", LOG_NAME))
            return {"CANCELLED"}
        topology = "geometry:" in out

        if spectral_changed:
            if topology:
                problems.append("spectral changes skipped: the material geometry changed in the same "
                                "install; edit the spectral realm again after this reload")
            else:
                spec_obj = os.path.join(edit_dir, unit + "_spectral.obj")
                _export_obj(spec_obj, [o for o in spec_col.all_objects if o.type == "MESH"], context, False, spec_col)
                code, out2 = _run("levelpatch", "--disc", _disc_path(), unit, "--from-mods",
                                  "--install", _mods_dir(), "--spectral-obj", spec_obj,
                                  "--spectral-map", os.path.join(work, unit + "_spectral.obj.map.json"))
                problems += _problems(out2)
                if code or "CHECK FAILED" in out2:
                    self.report({"ERROR"}, "Material realm installed, spectral realm not (see the %s text)"
                                % LOG_NAME)
                    open_unit(context, unit)
                    return {"CANCELLED"}

        ok, msg = open_unit(context, unit)        # reload: the next install needs fresh vertex maps
        if problems:
            self.report({"WARNING"}, "Installed %s, with warnings: %s (see the %s text)" % (
                unit, problems[0], LOG_NAME))
        else:
            self.report({"INFO"}, "Installed %s into the game%s" % (
                unit, "" if material_changed or spectral_changed else " (geometry unchanged)"))
        return {"FINISHED"}


class REWR_OT_check_budget(_ToolOperator, bpy.types.Operator):
    """Check whether every object type near this unit gets video memory for its textures
    (as installed; install first to check your changes)"""
    bl_idname = "rewraithed.check_budget"
    bl_label = "Check object memory"

    def execute(self, context):
        if not self._ready():
            return {"CANCELLED"}
        unit = context.scene.rewr_open_unit or context.scene.rewr_unit.strip().lower()
        _log("Object video memory near %s" % unit, clear=True)
        code, out = _run("vrambudget", "--disc", _disc_path(), "--install", _mods_dir(), unit)
        over = [ln for ln in out.splitlines() if "OVER" in ln]
        if over:
            self.report({"WARNING"}, over[0].strip())
        else:
            self.report({"INFO"}, "Every object type near %s fits" % unit)
        return {"FINISHED"}


class REWR_OT_launch(_ToolOperator, bpy.types.Operator):
    """Start the game in this unit"""
    bl_idname = "rewraithed.launch"
    bl_label = "Play this unit"

    def execute(self, context):
        if not self._ready():
            return {"CANCELLED"}
        s = context.scene
        unit = s.rewr_open_unit or s.rewr_unit.strip().lower()
        cmd = [os.path.join(_game_dir(), "rewraithed.exe"), "-area", unit, "-skipmenu", "-skiplogos"]
        if s.rewr_play_material:
            cmd.append("-material")
        if s.rewr_play_abilities:
            cmd += ["-abilities", "0x%X" % ALL_ABILITIES]
        if s.rewr_play_skipcutscene:
            cmd.append("-skipcutscene")
        if s.rewr_play_vramlog:
            cmd.append("-vramlog")
        subprocess.Popen(cmd, cwd=_game_dir())
        self.report({"INFO"}, "Starting the game in %s" % unit)
        return {"FINISHED"}


class REWR_OT_revert(_ToolOperator, bpy.types.Operator):
    """Delete your edited version of this unit from the mods folder, so the game uses the disc's
    again (object files and other units' files stay)"""
    bl_idname = "rewraithed.revert"
    bl_label = "Back to the original"
    bl_options = {"UNDO"}        # Ctrl+Z must not step back past unrecorded changes

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        if not self._ready():
            return {"CANCELLED"}
        unit = context.scene.rewr_open_unit
        if not unit:
            self.report({"ERROR"}, "Open a unit first")
            return {"CANCELLED"}
        removed = []
        for ext in ("drm", "crm"):
            p = os.path.join(_mods_dir(), *_level_path(unit, ext).split("\\"))
            if os.path.isfile(p):
                os.remove(p)
                removed.append(os.path.basename(p))
        _log("Reverted %s: removed %s" % (unit, ", ".join(removed) or "nothing"), clear=True)
        open_unit(context, unit)
        self.report({"INFO"}, "%s is back to the disc's version" % unit if removed
                    else "%s had no edited version" % unit)
        return {"FINISHED"}


class REWR_OT_show_log(bpy.types.Operator):
    """Show what the tools printed"""
    bl_idname = "rewraithed.show_log"
    bl_label = "Show log"

    def execute(self, context):
        t = bpy.data.texts.get(LOG_NAME) or bpy.data.texts.new(LOG_NAME)
        area = None
        for a in context.screen.areas:
            if a.type == "TEXT_EDITOR":
                area = a
        if area is None:
            # open it in a new window
            bpy.ops.wm.window_new()
            area = bpy.context.window_manager.windows[-1].screen.areas[0]
            area.type = "TEXT_EDITOR"
        area.spaces[0].text = t
        return {"FINISHED"}


# --- panel ----------------------------------------------------------------------

class REWR_PT_panel(bpy.types.Panel):
    bl_label = "Rewraithed"
    bl_idname = "REWR_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Rewraithed"

    def draw(self, context):
        s = context.scene
        lay = self.layout
        problem = _setup_problem()
        if problem:
            lay.label(text=problem, icon="ERROR")
            lay.operator("preferences.addon_show", text="Open preferences").module = _addon_key()
            return

        box = lay.box()
        box.label(text="1. Unit", icon="WORLD")
        row = box.row(align=True)
        if len(s.rewr_units):
            row.prop_search(s, "rewr_unit", s, "rewr_units", text="", icon="WORLD")
        else:
            row.prop(s, "rewr_unit", text="")
        row.operator("rewraithed.open_unit", text="Open")
        if s.rewr_open_unit:
            box.label(text="Editing: %s" % s.rewr_open_unit)
            row = box.row(align=True)
            row.prop(s, "rewr_realm", expand=True)

        if s.rewr_open_unit:
            box = lay.box()
            box.label(text="2. Objects", icon="GHOST_ENABLED")
            box.prop(s, "rewr_lib_kind", text="")
            box.template_list("REWR_UL_library", "", s, "rewr_library", s, "rewr_library_index", rows=8)
            if 0 <= s.rewr_library_index < len(s.rewr_library):
                typ = s.rewr_library[s.rewr_library_index].name
                icon = _model_preview(typ)
                if icon:
                    box.template_icon(icon_value=icon, scale=7.0)
                else:
                    box.label(text="No model to show (an effect or helper)", icon="INFO")
                box.label(text="Double-click: add at the 3D cursor", icon="MOUSE_LMB_2X")
            row = box.row(align=True)
            row.label(text="Snap")
            row.prop(s, "rewr_snap", expand=True)
            if s.rewr_snap == "GRID":
                box.prop(s, "rewr_grid")
            row = box.row(align=True)
            row.prop(s, "rewr_labels", toggle=True, icon="SMALL_CAPS")
            row.operator("rewraithed.drop_to_floor", icon="TRIA_DOWN_BAR")
            row.operator("rewraithed.refresh_library", text="", icon="FILE_REFRESH")

            box = lay.box()
            box.label(text="3. Into the game", icon="PLAY")
            box.operator("rewraithed.install", icon="IMPORT")
            box.operator("rewraithed.check_budget", icon="MEMORY")
            col = box.column(align=True)
            col.operator("rewraithed.launch", icon_value=_play_icon())
            row = col.row(align=True)
            row.prop(s, "rewr_play_material", toggle=True)
            row.prop(s, "rewr_play_abilities", toggle=True)
            row = col.row(align=True)
            row.prop(s, "rewr_play_skipcutscene", toggle=True)
            row.prop(s, "rewr_play_vramlog", toggle=True)
            row = box.row()
            row.alert = True                      # red: it throws your version away
            row.operator("rewraithed.revert", icon="LOOP_BACK")
        lay.operator("rewraithed.show_log", icon="TEXT")


class REWR_UnitItem(bpy.types.PropertyGroup):
    pass


class REWR_LibraryItem(bpy.types.PropertyGroup):
    kind: StringProperty()
    count: IntProperty()


class REWR_UL_library(bpy.types.UIList):
    """Every object type in the game; type in the search box to find one"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0):
        split = layout.split(factor=0.8)
        name = split.row()
        name.alignment = "LEFT"
        op = name.operator("rewraithed.pick_type", text=item.name, icon=KIND_ICON.get(item.kind, "OBJECT_DATA"),
                           emboss=False)
        op.library_index = index
        count = split.row()
        count.alignment = "RIGHT"
        count.label(text="%d" % item.count)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        kind = context.scene.rewr_lib_kind
        want = self.filter_name.lower()
        flags = []
        for it in items:
            ok = (kind == "ALL" or it.kind == kind) and (not want or want in it.name.lower())
            flags.append(self.bitflag_filter_item if ok else 0)
        return flags, []


CLASSES = (RewraithedPreferences, REWR_UnitItem, REWR_LibraryItem, REWR_UL_library, REWR_OT_open_unit,
           REWR_OT_pick_type, REWR_OT_add_object,
           REWR_OT_drop_to_floor, REWR_OT_refresh_library, REWR_OT_install, REWR_OT_check_budget,
           REWR_OT_launch, REWR_OT_revert, REWR_OT_show_log, REWR_PT_panel)

SCENE_PROPS = ("rewr_unit", "rewr_units", "rewr_open_unit", "rewr_realm", "rewr_library", "rewr_library_index",
               "rewr_lib_kind", "rewr_snap", "rewr_grid", "rewr_play_material", "rewr_play_abilities", "rewr_labels",
               "rewr_play_skipcutscene", "rewr_play_vramlog")


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    S = bpy.types.Scene
    S.rewr_unit = StringProperty(name="Unit", default="city9",
                                 description="Unit to open: click to pick from every unit in the game, or type to search")
    S.rewr_open_unit = StringProperty(name="Open unit")
    S.rewr_units = CollectionProperty(type=REWR_UnitItem)
    S.rewr_library = CollectionProperty(type=REWR_LibraryItem)
    S.rewr_library_index = IntProperty(name="Object type")
    S.rewr_lib_kind = EnumProperty(
        name="Show", description="Which kind of object the library lists",
        items=[("ALL", "Everything", "Every object type", "NONE", 0)] +
              [(k, label, label, icon, i + 1) for i, (k, _, icon, label) in enumerate(KINDS) if k != "marker"])
    S.rewr_snap = EnumProperty(
        name="Snap", update=_snap_update, default="GRID",
        items=[("OFF", "Off", "Move freely"),
               ("GRID", "Grid", "Moving snaps to the grid, turning to 15 degree steps"),
               ("SURFACE", "Surface", "Moving sticks to the ground and walls under the mouse")])
    S.rewr_grid = FloatProperty(name="Grid size (m)", default=0.5, min=0.05, max=10.0, update=_snap_update,
                                description="Grid spacing; 1 m is 100 game units")
    S.rewr_realm = EnumProperty(name="Realm", items=[("MATERIAL", "Material", "Show the material realm"),
                                                     ("SPECTRAL", "Spectral", "Show the spectral realm")],
                                update=_realm_update)
    S.rewr_play_material = BoolProperty(name="Material", description="Start in the material realm")
    S.rewr_play_abilities = BoolProperty(name="All abilities",
                                         description="Raziel starts with every ability, Reaver and glyph "
                                                     "(swimming, wall climbing, passing barriers...)")
    S.rewr_labels = BoolProperty(name="Labels", default=True, update=_redraw_views,
                                 description="Show each object's name in a box above it")
    S.rewr_play_skipcutscene = BoolProperty(
        name="No cutscenes",
        description="Skip every cutscene: scenes play out too fast to see (so doors, enemies and the rest "
                    "end up as the scene leaves them) and script-started movies are left out")
    S.rewr_play_vramlog = BoolProperty(name="Memory log",
                                       description="Log object video memory to vram.log next to the game")
    bpy.types.UI_MT_button_context_menu.prepend(_list_context_menu)
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.timers.register(_startup, first_interval=0.5)
    _LABELS["handle"] = bpy.types.SpaceView3D.draw_handler_add(_draw_labels, (), "WINDOW", "POST_PIXEL")


def _fill_scene_lists():
    """The unit and library lists, from the game's object list (built the first time)."""
    if _setup_problem() is None and bpy.context.scene is not None:
        try:
            _load_catalog()
        except Exception as ex:
            print("rewraithed: could not read the object list: %s" % ex)


def _startup():
    _fill_scene_lists()
    return None          # once


@persistent
def _on_load(_):
    # a new or opened .blend has its own scene, without the lists
    bpy.app.timers.register(_startup, first_interval=0.1)


def unregister():
    if _LABELS["handle"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_LABELS["handle"], "WINDOW")
        _LABELS["handle"] = None
    bpy.types.UI_MT_button_context_menu.remove(_list_context_menu)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    if bpy.app.timers.is_registered(_startup):
        bpy.app.timers.unregister(_startup)
    if _PREVIEWS["coll"] is not None:
        bpy.utils.previews.remove(_PREVIEWS["coll"])
        _PREVIEWS["coll"] = None
    S = bpy.types.Scene
    for p in SCENE_PROPS:
        if hasattr(S, p):
            delattr(S, p)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
