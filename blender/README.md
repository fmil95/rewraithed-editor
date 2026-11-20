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

Click the unit box to pick from every unit in the game (type part of a name, like
`under`, to narrow the list), then press **Open**. This clears what the
editor loaded before, so unsaved Blender changes to the previous unit are lost. If you
already installed an edited version of the unit, that version opens, so your changes
build on each other.

The first time, it also reads every object type in the game for the library (a few
seconds).

You get three collections:

| collection | what it is |
|---|---|
| `city9 material` | the terrain as it looks in the material realm |
| `city9 spectral` | the same terrain in the spectral realm |
| `city9 objects` | enemies, objects and Raziel's start, each drawn as its model |

The **Material / Spectral** buttons switch which terrain you see. The unit opens
textured and lit the way the game draws it (each texture tinted by the vertex colours,
sharp pixels). If the textures disappear, switch the 3D view back to *Material
Preview* (the third sphere at the top right). Fly through with **Shift+`**
(W/A/S/D, mouse to look) or use the numpad keys for views from above.

Each object shows as its own model in its own colour, with its name in a box above
it (orange when selected; far away ones fade out, and **Labels** turns them off), and a
small pointer on the ground showing which way it faces. Raziel's start is green,
effects with nothing to see (flames, sounds, helpers) are white diamonds, and the
unit's path and camera markers are small grey diamonds. Some objects float a little
above the floor; that's where the game puts them, and they drop when they appear.

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
* Click an object, then **G** to move it and **R** to turn it. Turning only goes
  around the vertical axis, the way the game turns them.
* **X** deletes it.
* **Shift+D** copies it, with all its settings.

**The object library** (*2. Objects*) lists every object type in the game (375 of
them), with how many the game has. Pick a kind at the top (creatures, bosses,
objects, effects, Raziel's start) or type part of a name in the search box at the
bottom of the list. Hovering over a type says where the game uses it; clicking it
shows a picture of its model below the list. To place one:

1. **Shift+right-click** on the ground where you want it; this puts the 3D cursor
   (the red and white ring) there.
2. **Double-click** the type in the list, or right-click it and pick **Add ... at the
   3D cursor**. It appears standing on the floor, selected, so **G** and **R** move
   and turn it straight away.

A type the unit doesn't have yet is copied, with its files, from a unit that has one
when you install. It keeps its behaviour, but not its patrol route.

**Snapping**, below the library:
* **Grid**: moving snaps to a grid (set its size: 0.5 m is 50 game units) and turning
  goes in 15 degree steps. Works in Edit Mode on the terrain too.
* **Surface**: a moved object sticks to the floor or wall under the mouse.
* **Off**: move freely.
* **Drop to floor** stands the selected objects on the terrain under them.
* **Ctrl+Z** undoes the last thing you did, including adding an object or opening a
  unit.
* The refresh button reads the list of object types again; you only need it after
  installing units with types the game didn't have there before.

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
  material realm, give Raziel every ability, Reaver and glyph (**All abilities**:
  swimming is needed for some units, such as `city20` in the material realm), skip the opening cutscene (under1's), or write a memory log (`vram.log`).
  Starting in a unit needs Raziel's start in it. Not every original unit has one
  (`city19` and `city20` don't), so add one from the library (*Raziel's start*), or
  walk in from a neighbour.
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
* Edit what enemies do (patrols, scripts). Enemies from the library keep their
  behaviour but have no patrol route.
* Show objects with their textures (they're drawn in one colour each).
* Turn or move a whole unit.
