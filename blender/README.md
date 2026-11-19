# Rewraithed level editor for Blender

A panel in Blender for editing the game's levels. You open a unit (a piece of a level,
like `city9`), change it in Blender, and press **Install into the game**. The next
time the game loads that unit, it shows your version. Your changes go into the `mods`
folder next to `rewraithed.exe`; the game disc is never changed.

## Setting it up (once)

1. Blender 4.2 or newer (tested with 5.3).
2. *Edit > Preferences > Add-ons*, the arrow menu at the top right, **Install from Disk...**,
   and pick `editor/blender/rewraithed_editor.py`. Tick the add-on to turn it on.
3. Open the add-on's settings (the arrow next to its name) and fill in:
   * **Game folder**: the folder with `rewraithed.exe` (for example `build/Debug`).
   * **Disc image**: `KAIN2.img`. Leave it empty if it is in `<game folder>/disc/`.
   * **Editor tools**: the `editor/leveldump` folder of the repository.
   It says **Ready** when everything is found.
4. In the 3D view, press **N** and pick the **Rewraithed** tab.

## Editing a unit

### 1. Open it

Type the unit's name (`city9`, `under1`...) and press **Open**. This clears what the
editor loaded before, so unsaved Blender changes to the previous unit are lost. If you
already installed an edited version of the unit, that version opens, so your changes
build on each other.

You get three collections:

| collection | what it is |
|---|---|
| `city9 material` | the terrain as it looks in the material realm |
| `city9 spectral` | the same terrain in the spectral realm |
| `city9 objects` | enemies, objects and Raziel's start, one small arrow each |

The **Material / Spectral** buttons switch which terrain you see. The unit opens
textured and lit the way the game draws it (each texture tinted by the vertex colours,
sharp pixels), with each object arrow labelled. If the textures disappear, switch the
3D view back to *Material Preview* (the third sphere at the top right). Fly through
with **Shift+`** (W/A/S/D, mouse to look) or use the numpad keys for views from above.

### 2. Change things

**Terrain, material realm** (`material` collection):
* Move vertices in Edit Mode (Tab) to reshape the ground and walls.
* Add or delete geometry: extrude, delete faces, add new pieces. New faces copy their
  texture from the face next to them.
* Change textures: give faces one of the `tex_...` materials and move their UVs. For
  a new picture, use an ordinary material with an image texture (a PNG).
* Paint vertex colours (Vertex Paint mode) for lighting.

**Terrain, spectral realm** (`spectral` collection): move vertices and paint colours,
but don't add or delete anything here. If you added or deleted geometry in the
material realm, install that first; after the reload, the spectral realm has the new
geometry and you can shape it.

**Objects** (`objects` collection):
* Move an arrow to move the object, and rotate it around Z to turn it. The arrow's tip
  is the way the object faces.
* Delete an arrow to remove the object.
* Duplicate an arrow (**Shift+D**) to add another of the same kind.

**Enemies from another unit**: in *2. Objects from another unit*, type a unit (for
example `city4`, which has hunters) and press **Bring**. Its objects appear in an
`objects from city4` collection, where they stand in that unit, so they may be far
away. To use one, select it, press **M** and pick `city9 objects`. Then move it to
where you want it. To use several of the same kind, duplicate it first (**Shift+D**,
then **M**). Anything left in `objects from city4` is ignored.

Rules that keep the level loading:
* Don't rename or join objects in the `material` and `spectral` collections.
* Don't leave holes: the game can crash when Raziel walks into a gap in the walls or
  floor.
* Keep portals (the `portal...` objects, the doorways to other units) where they are.

### 3. Into the game

* **Install into the game** writes the unit, checks it, and reloads it in Blender.
  Problems show at the bottom of the window and in the **Rewraithed log** text
  (**Show log**). A unit that fails its checks is not written.
* **Check object memory**: every kind of enemy or object near a unit needs its own
  share of a small piece of video memory, shared with the units next to it. When it
  runs out, enemies show with scrambled textures. Install already runs this check and
  warns you; a message like "city19 ... OVER, no room for hunter" means there are too
  many *different kinds* nearby. More of a kind already there costs nothing.
* **Play this unit** starts the game in the unit. The buttons below it start it in the
  material realm, let Raziel swim (needed for some units, such as `city20` in the
  material realm), skip the unit's cutscene, or write a memory log (`vram.log`).
  Starting in a unit needs a Raziel arrow in it. Not every original unit has one
  (`city19` and `city20` don't), so add one, or walk in from a neighbour.
* **Back to the original** deletes your version of the unit, so the game uses the
  disc's again.

## Where things are

* Your edited units: `<game folder>/mods/kain2/area/<area>/bin/<unit>.drm` (and `.crm`
  for textures). Object files copied for new enemies go to `mods/kain2/object/`.
* The editor's working files: `<game folder>/editor-work/` (safe to delete).
* Back up the `mods` folder to keep a version of your work, and empty it to go back to
  the original game.

## What it can't do yet

* Make a new unit or new doorways between units (the command-line tools `newunit.py`
  and `units.py` can; see `../leveldump/README.md`).
* Edit what enemies do (patrols, scripts). Copied enemies keep their behaviour but
  lose the patrol markers from their old unit.
* Turn or move a whole unit.
