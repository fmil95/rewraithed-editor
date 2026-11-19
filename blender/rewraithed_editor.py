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
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > Rewraithed",
    "description": "Edit Soul Reaver prototype levels for the rewraithed PC port",
    "category": "Import-Export",
}

import hashlib
import os
import re
import shutil
import subprocess
import sys

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

LOG_NAME = "Rewraithed log"
SCALE = 0.01


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
    for o in list(c.all_objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for ch in list(c.children):
        _remove_collection(ch.name)
    bpy.data.collections.remove(c)


def _clear_editor_scene():
    for c in list(bpy.data.collections):
        if c.get("rewraithed"):
            _remove_collection(c.name)
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


def _set_realm(scene, realm):
    unit = scene.rewr_open_unit
    for part, show in (("material", realm == "MATERIAL"), ("spectral", realm == "SPECTRAL")):
        lc = _layer_collection(bpy.context.view_layer, _col_name(unit, part))
        if lc:
            lc.hide_viewport = not show


def _realm_update(self, context):
    _set_realm(context.scene, self.rewr_realm)


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

    _clear_editor_scene()
    root = _collection(scene, "Rewraithed: " + unit)
    root["rewraithed"] = True
    cols = {}
    for part in ("material", "spectral", "objects"):
        c = _collection(scene, _col_name(unit, part), root)
        c["rewraithed"] = True
        cols[part] = c
    _import_obj(os.path.join(out_dir, unit + "_material.obj"), cols["material"], context, True)
    _import_obj(os.path.join(out_dir, unit + "_spectral.obj"), cols["spectral"], context, True)
    if os.path.isfile(os.path.join(out_dir, unit + "_intros.obj")):
        _import_obj(os.path.join(out_dir, unit + "_intros.obj"), cols["objects"], context)

    scene.rewr_open_unit = unit
    scene.rewr_templates = ""
    scene["rewr_fp_material"] = _fingerprint(cols["material"].objects)
    scene["rewr_fp_spectral"] = _fingerprint(cols["spectral"].objects)
    _set_realm(scene, scene.rewr_realm)
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


class REWR_OT_add_template_unit(_ToolOperator, bpy.types.Operator):
    """Bring in another unit's objects and enemies, to copy into this unit: move (M) or
    duplicate (Shift+D, then M) an arrow into this unit's objects collection"""
    bl_idname = "rewraithed.add_template_unit"
    bl_label = "Bring objects from unit"

    def execute(self, context):
        if not self._ready():
            return {"CANCELLED"}
        scene = context.scene
        unit = scene.rewr_open_unit
        src = scene.rewr_template_unit.strip().lower()
        if not unit:
            self.report({"ERROR"}, "Open a unit first")
            return {"CANCELLED"}
        if not re.fullmatch(r"[a-z]+[0-9]+", src) or src == unit:
            self.report({"ERROR"}, "Give another unit's name, e.g. city4")
            return {"CANCELLED"}
        out_dir = _work_dir("templates", src)
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)
        code, out = _run("leveldump", "--disc", _disc_path(), "--mods", _mods_dir(), src,
                         "--obj", out_dir, "--no-textures")
        path = os.path.join(out_dir, src + "_intros.obj")
        if code or not os.path.isfile(path):
            self.report({"ERROR"}, "Could not read %s (see the %s text)" % (src, LOG_NAME))
            return {"CANCELLED"}
        root = bpy.data.collections.get("Rewraithed: " + unit)
        name = "objects from %s" % src
        _remove_collection(name)
        c = _collection(scene, name, root)
        c["rewraithed"] = True
        new = _import_obj(path, c, context)
        # keep only placeable things: markers and camera paths belong to their unit
        for o in list(new):
            if o.name.endswith(("_marker", "_campath", "_raziel")):
                bpy.data.objects.remove(o, do_unlink=True)
        tmpl = [t for t in scene.rewr_templates.split(",") if t]
        if src not in tmpl:
            tmpl.append(src)
        scene.rewr_templates = ",".join(tmpl)
        self.report({"INFO"}, "%d objects from %s; they sit where they are in %s" % (len(c.objects), src, src))
        return {"FINISHED"}


class REWR_OT_install(_ToolOperator, bpy.types.Operator):
    """Write the edited unit into the game's mods folder, check it, and reload it here"""
    bl_idname = "rewraithed.install"
    bl_label = "Install into the game"

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

        edit_dir = os.path.join(work, "edited")
        os.makedirs(edit_dir, exist_ok=True)
        mat_obj = os.path.join(edit_dir, unit + "_material.obj")
        intros_obj = os.path.join(edit_dir, unit + "_intros.obj")
        _export_obj(mat_obj, [o for o in mat_col.all_objects if o.type == "MESH"], context, True, mat_col)
        arrows = [o for o in obj_col.all_objects if o.type == "MESH" and o.name.startswith("intro_")]
        _export_obj(intros_obj, arrows, context, False, obj_col)

        spectral_changed = spec_col is not None and \
            _fingerprint(spec_col.objects) != scene.get("rewr_fp_spectral")
        material_changed = _fingerprint(mat_col.objects) != scene.get("rewr_fp_material")

        args = ["--disc", _disc_path(), unit, "--from-mods", "--install", _mods_dir(),
                "--material-obj", mat_obj, "--map", os.path.join(work, unit + "_material.obj.map.json"),
                "--intros-obj", intros_obj]
        if scene.rewr_templates:
            args += ["--intro-templates", scene.rewr_templates]
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

        templates = scene.rewr_templates
        ok, msg = open_unit(context, unit)        # reload: the next install needs fresh vertex maps
        if templates:
            _log("(objects brought from %s are cleared after installing; bring them again to copy more)"
                 % templates)
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
        if s.rewr_play_swim:
            cmd.append("-swim")
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
        row.prop(s, "rewr_unit", text="")
        row.operator("rewraithed.open_unit", text="Open")
        if s.rewr_open_unit:
            box.label(text="Editing: %s" % s.rewr_open_unit)
            box.prop(s, "rewr_realm", expand=True)

        if s.rewr_open_unit:
            box = lay.box()
            box.label(text="2. Objects from another unit", icon="GHOST_ENABLED")
            row = box.row(align=True)
            row.prop(s, "rewr_template_unit", text="")
            row.operator("rewraithed.add_template_unit", text="Bring")
            if s.rewr_templates:
                box.label(text="From: " + s.rewr_templates.replace(",", ", "))

            box = lay.box()
            box.label(text="3. Into the game", icon="PLAY")
            box.operator("rewraithed.install", icon="IMPORT")
            box.operator("rewraithed.check_budget", icon="MEMORY")
            col = box.column(align=True)
            col.operator("rewraithed.launch", icon="PLAY")
            row = col.row(align=True)
            row.prop(s, "rewr_play_material", toggle=True)
            row.prop(s, "rewr_play_swim", toggle=True)
            row = col.row(align=True)
            row.prop(s, "rewr_play_skipcutscene", toggle=True)
            row.prop(s, "rewr_play_vramlog", toggle=True)
            box.operator("rewraithed.revert", icon="LOOP_BACK")
        lay.operator("rewraithed.show_log", icon="TEXT")


CLASSES = (RewraithedPreferences, REWR_OT_open_unit, REWR_OT_add_template_unit, REWR_OT_install,
           REWR_OT_check_budget, REWR_OT_launch, REWR_OT_revert, REWR_OT_show_log, REWR_PT_panel)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    S = bpy.types.Scene
    S.rewr_unit = StringProperty(name="Unit", default="city9", description="Unit to open, e.g. city9")
    S.rewr_open_unit = StringProperty(name="Open unit")
    S.rewr_template_unit = StringProperty(name="From unit", default="city4",
                                          description="Unit to copy objects and enemies from")
    S.rewr_templates = StringProperty(name="Template units")
    S.rewr_realm = EnumProperty(name="Realm", items=[("MATERIAL", "Material", "Show the material realm"),
                                                     ("SPECTRAL", "Spectral", "Show the spectral realm")],
                                update=_realm_update)
    S.rewr_play_material = BoolProperty(name="Material", description="Start in the material realm")
    S.rewr_play_swim = BoolProperty(name="Swim", description="Raziel can swim")
    S.rewr_play_skipcutscene = BoolProperty(name="No cutscene", description="Skip the unit's cutscene")
    S.rewr_play_vramlog = BoolProperty(name="Memory log",
                                       description="Log object video memory to vram.log next to the game")


def unregister():
    S = bpy.types.Scene
    for p in ("rewr_unit", "rewr_open_unit", "rewr_template_unit", "rewr_templates", "rewr_realm",
              "rewr_play_material", "rewr_play_swim", "rewr_play_skipcutscene", "rewr_play_vramlog"):
        if hasattr(S, p):
            delattr(S, p)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
