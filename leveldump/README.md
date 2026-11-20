# leveldump

Reads a Soul Reaver (prototype) level `.drm` the way the game loads it and
reports what is inside: the `Level` header, the `Terrain`, stream portals,
intros, spectral morph data and every BSP tree. It can also export the terrain
as OBJ files (one for the material realm, one for the spectral realm) for Blender.

This is the read-only first step of the level editor. It needs nothing beyond
Python 3.8+ and never writes into the game data.

## Usage

```
python leveldump.py --disc <path>/KAIN2.img under1
python leveldump.py --disc <path>/KAIN2.img under1 city9 --obj out/
python leveldump.py --disc <path>/KAIN2.img under1 --nodes --all-intros
python leveldump.py --disc <path>/KAIN2.img under1 --json out/under1.json --extract out/
python leveldump.py --drm out/under1.drm
```

| option | |
|---|---|
| `--disc IMG units...` | CD image (raw MODE2/2352 `.img`/`.bin` or 2048-byte `.iso`); levels are read from `BIGFILE.DAT` inside it |
| `--drm FILE...` | already extracted `.drm` files instead |
| `--obj DIR` | write `<unit>_material.obj` and `<unit>_spectral.obj` |
| `--scale F` | OBJ scale (default 0.01; areas span tens of thousands of game units) |
| `--json PATH` | dump everything (vertices, faces, intros, nodes, leaves...) as JSON |
| `--extract DIR` | save the raw `.drm` read from the disc |
| `--nodes` | print every BSP node and leaf |
| `--all-intros` | also list `marker` / `campath` intros |
| `--no-check` | skip recomputing bounds from the geometry |

Unit names are the stream unit names used by the game (`under1`, `city9`,
`cathy12`...). Portals list their destination units, so you can walk the world
from `under1`.

In Blender: *File > Import > Wavefront (.obj)*, choose **Z up, Y forward**
(the game is Z up). Each BSP tree is a separate object named
`tree<index>_id<ID>_flags<flags>` (plus `..._backfaces` for the reverse side
of double-sided faces); portals are separate `portal<n>_<dest>` quads.
Vertex colours are written as `v x y z r g b`; they show up as a colour attribute.

## Editing a level (levelpatch.py)

`levelpatch.py` writes a modified `.drm` that keeps the level's vertices, faces
and BSP trees, and rebuilds what depends on vertex positions: face normals
(appending to the normal list when needed), the morph data's material
positions, and the spheres, boxes and plane error fields of every BSP leaf and
node above a changed face. If the OBJ adds or deletes geometry, see
"Adding and deleting geometry" below.

```
# 1. export (writes under1_material.obj and under1_material.obj.map.json)
python leveldump.py --disc <path>/KAIN2.img under1 --obj work/
# 2. Blender: import with Z up / Y forward, move vertices (do not add or delete
#    any, do not rename or join objects), export OBJ with Z up / Y forward,
#    Colors on if you painted vertex colours
# 3. write the level into the port's mods folder
python levelpatch.py --disc <path>/KAIN2.img under1 --material-obj edited.obj \
    --map work/under1_material.obj.map.json --install <rewraithed exe folder>/mods
```

To build on an earlier edit, export with `leveldump.py --mods <exe folder>/mods` and
patch with `levelpatch.py ... --install <exe folder>/mods --from-mods`. Both then read
units and object files from the mods folder where they are there, like the port does.
The Blender add-on (`../blender/`) works this way.

`--hill X Y RADIUS HEIGHT [--tint R G B]` is a scripted test edit that needs no
Blender. Every output is re-read and checked before it's written. An OBJ
exported by Blender without changes patches back to a byte-identical level.

The port loads `mods\kain2\area\under\bin\under1.drm` next to `rewraithed.exe`
in place of the copy in `BIGFILE.DAT`; delete the file to go back. For quick
tests: `rewraithed.exe -area under1 -skipmenu -skiplogos -skipcutscene`.

### Adding and deleting geometry

When the edited OBJ adds or deletes vertices, faces or objects, `levelpatch.py`
switches to `geometry.py` automatically, then recompiles every BSP tree:

* Original vertices keep their indices (morph entries and VMObjects, the
  animated water and colour effects, refer to them by index). Deleted vertices
  stay in the list unused; new vertices are appended. OBJ vertices are matched
  by index when an object only gained vertices (Blender appends new ones), or
  by exact position when some were deleted. A vertex that was moved in an
  object that also lost vertices can't be identified and becomes a new vertex.
* A face with the same vertices, in the same order, as an original face keeps that
  face's record (flags, texture, sort push). A new face copies them from the
  original face of its tree that shares most vertices with it, so it gets a
  neighbour's texture, stretched (texture coordinates aren't edited yet).
* New vertices take their colour from the OBJ, or from the nearest original
  vertex; they have no spectral morph (same position in both realms) and take
  the nearest original vertex's spectral colour.
* OBJ object `tree<N>_...` (and its `_backfaces` twin) holds tree N's faces. A new
  object goes to tree 0; deleting an object deletes its faces. Portal objects are ignored.
* Export from Blender with "Triangulated Mesh" on; other polygons are fan-triangulated.

Tested with Blender: extruding 5 floor triangles of `under1` into a platform
300 units high (7 new vertices, 19 new faces), and deleting the same 5 faces.
Both pass all checks and load in the port.

### Different shapes in the two realms

A vertex's spectral position comes from its morph entry (`MorphDiffList`:
material position plus spectral delta) and its spectral colour from
`MorphColorList`. To give geometry its own spectral look:

1. add or edit it in the material OBJ and apply that (`--material-obj`);
2. export the result again, edit the *spectral* OBJ in Blender (move vertices,
   paint vertex colours; don't add or delete anything) and apply it with
   `--spectral-obj ... --spectral-map ...`.

A vertex without a morph entry that gets a different spectral position now gets
one: new entries go in front of the list's end entry (`vindex` < 0), the block
being padded after a fresh end entry. Bounds, error fields and spectral normals
are rebuilt for both realms, so collision follows the spectral shape there too.

Tested in `city9`: an obelisk next to Raziel's start, straight and brick-textured
in the material realm, twisted, leaning, 35 % taller and green in the spectral realm.

Trigger faces (attr & `0xC0`) keep their signal offset in every edit: texture
handling never touches them (an earlier version would have cleared them when
the OBJ marked them "untextured", disconnecting the unit's portals), and every
tool checks that each trigger face points at a `MultiSignal`.

### Textures

With `--disc`, `leveldump.py --obj` also writes `<unit>.mtl` and
`<unit>_textures/page_<tpage>_<clut>.png`, and gives every textured face UVs and a
material `tex_<tpage>_<clut>` (`--no-textures` leaves them out; for a `--drm`
file pass the unit's `.crm` with `--crm`). In Blender (material preview or
texture shading) the level shows its real textures. They're mostly grey: the game
tints them with the vertex colours.

Edit textures by giving faces one of the imported `tex_...` materials and moving
their UVs within that 256x256 page (export with UVs and materials on). Then:

* a face whose material and UVs still match its texture entry keeps it;
* otherwise an identical entry is reused, or a new one is appended to the
  texture list (`TextureFT3`, at most about 5,400 entries per unit). A new entry
  keeps the old entry's flag bits (`attr`: 0x1000, 0x2000, 0x4000, 0x8000, which
  collision and physics read);
* material `untextured` removes a face's texture; a face without UVs keeps its
  texture (new faces: a neighbour's).

UVs are rounded to whole texels (u = U * 256, v = (1 - V) * 256, 0..255).

**New images.** Give faces an ordinary Blender material with an image texture
(an 8-bit PNG) and UV-map them over the image (0..1). Export with materials and
UVs and "Path Mode: Absolute" (or keep the image next to the .mtl). On import
(`newtex.py`, `vramspace.py`):

* the image is shrunk if needed (at most 128 texels, keeping its aspect) to
  fit a free rectangle of the unit's video-memory block inside one texture page,
  reduced to 16 colours (median cut on its distinct colours; exact when it has 16
  or fewer), and written as a 4-bit texture with its palette in a free palette slot;
* transparent pixels (alpha < 128) become colour 0, which the PlayStation doesn't draw;
* the faces' UVs are mapped into the placed rectangle (UVs outside 0..1 are
  clamped: terrain textures can't repeat);
* the patched `.crm` is written next to the `.drm` (with `--install`, into the
  mods folder, where the port loads it too). For a `--drm` input pass `--crm`.

Video memory is reloaded from a unit's `.crm` whenever Raziel enters that unit
(`STREAM_MoveIntoNewStreamUnit` -> `LoadLevels` -> `STREAM_LoadMainVram`), and the
neighbouring units seen through portals are drawn from that same memory. That's
why neighbouring `.crm` files agree on each other's texels (869 of 871 portal
links), while units further apart don't. So "free" means not touched by any
texture entry, palette or animated texture of the unit, of its portal neighbours
or of *their* neighbours, and the image is written into the unit's and each
neighbour's `.crm` (all installed into the mods folder; this needs `--disc`). In
`under1` that leaves room for about 56x54 texels. The game multiplies every texel by the
vertex colour (128 = unchanged), so a new image picks up the area's lighting:
in `under1`'s teal cave, white turns cyan. Paint the vertex colours grey
(128, 128, 128) in Blender to see it as drawn (material realm; spectral colours
come from the spectral data).

Tested in `under1`: a generated 128x128 picture (rings and a checker) on the
platform top, shown in the port. (A first version only checked `under1` itself
and overwrote textures of `under2`'s walls.)

How the textures are stored: a unit's `.crm` is a 36-byte header and raw 16-bit
video memory, uploaded to (`Level.vramSize.x` + 512, `vramSize.y`), `w` x `h`
halfwords (`STREAM.c`). `TextureFT3` holds `u0 v0 clut u1 v1 tpage u2 v2 attr`
for the face's corners v0, v1, v2; `tpage` and `clut` are the usual PlayStation
texture page and palette words. Every terrain page seen so far is 4-bit.

Tested in `under1`: the extruded platform's top re-textured in Blender with the
carved face from page `000c`/palette `34ba`, shown in the port.

### Objects and markers (intros)

`leveldump.py --obj` also writes `<unit>_intros.obj`: one small arrow per intro,
named `intro_<UniqueID>_<type>`. The arrow's base is the intro's position and its
tip points along its Z rotation (rotation 0 is drawn along +Y). Import it into the
same Blender scene as the terrain, then:

* **move or rotate an arrow** (Object Mode is fine) to move or turn the intro. A
  spline attached to it (camera paths, moving objects) moves along;
* **delete an arrow** to delete the intro;
* **duplicate an arrow** (Shift+D; Blender names it `....001`) to add another intro
  of that type, sharing the original's data block, without its spline;
* **bring in an arrow from another unit's `_intros.obj`** (e.g. `intro_4598_urnx`
  from `city9`) to add an object type that unit has. Pass the source units with
  `--intro-templates`.

```
python levelpatch.py --disc <path>/KAIN2.img under1 --intros-obj edited_intros.obj \
    --intro-templates city9 --install <rewraithed exe folder>/mods
```

`--intros-obj` combines with `--material-obj`. Existing UniqueIDs never change,
because scripts refer to intros by UniqueID; new intros get fresh ones. Deleting an
intro a script uses can break that script.

Object files (`\kain2\object\<type>\<type>.drm`, `.crm`) are stored in the bigfile
directory of each unit that uses the type, not in a shared place. A type copied
from another unit is missing from this unit's directory, and the game stops with
"file does not exist", so `--install` also copies the type's files from the
template unit into the mods folder and appends the type to `Level.objectNameList`.
An intro copied from another unit brings a copy of its data block, appended
to the level. Data blocks are lists of commands, `{short command, short count,
long parameter[count]}` ending with command 0 (`MON_ProcessIntro` in
`MONLIB.c`); all 724 in the game parse that way and none contains a pointer.
Commands that name things in the source unit are adjusted:

* 10 (patrol markers), 12 (ambush marker), 13 (flee marker) are dropped: they
  are planning-marker IDs, looked up in the monster's current unit;
* 15 (soul link, the UniqueID of the soul intro) is dropped; without it the game
  creates the soul when the monster dies;
* 14 (the stream units the monster may be in) becomes the destination unit;
* the rest (behaviours, hit points, ranges, age, the spectral flag, respawn) is kept.

Some objects need other object types loaded without placing one: every unit with
hunters also loads their weapon (`fthrow` or `xbow`). `--extra-objects
fthrow:city4` appends such types to the object list and copies their files.
Object sound files (`\kain2\sfx\object\<type>\<type>.snf`, `.smf`) are copied too.

Tested in `under1`: Raziel's start marker moved onto a new platform (he spawns on
it), an urn (`urnx`) copied from `city9`, and a sluagh (spectral-realm enemy) copied
from `city9` with its data block. In `city9`: two vwraiths (spectral realm, from
`city20`) and two flamethrower hunters (material realm, from `city4`, with
`--extra-objects fthrow:city4`) near Raziel's start.

### Portals and new units (units.py)

How a portal works:

* `Terrain.StreamUnits` lists `StreamUnitPortal` records: `"<unit>,<n>"` (the unit
  on the other side and the number of the portal there), this portal's own number
  (`MSignalID`), the other unit's stream ID, a box and two triangles. The streaming
  code loads the neighbour from it and lines the two units up.
* The crossing is two terrain faces in the tree with ID -1 (face attr `0x44`). For
  faces with attr & `0xC0`, `TFace.textoff` is an offset into `Terrain.signals`
  (a `MultiSignal`: `numSignals`, `signalNum`, `flags`, signals), not a texture;
  crossing the face fires it (`COLLIDE_LineWithSignals`). A portal's signal is id
  18, "stream level": `currentnum`, `streamID`, `toname[16]`, the same data as the
  record. Signal lists end with id 15; payload lengths are in `signalInfoList`.
* The MultiSignals between `Level.SignalListStart` and `SignalListEnd` (offsets
  `0xD0`, `0xD4`) are walked one after the other whenever the level moves in memory
  (`SIGNAL_RelocateSignal`, from `MEMPACK_RelocateAreaType`): 8-byte header,
  `numSignals` signals, a 4-byte end marker (id 15), the next one right after.
  An empty one is 12 bytes. Setting only `numSignals` to 0 leaves the old signals
  where the next MultiSignal is expected, and the game crashes the next time it
  tidies memory (e.g. when Raziel enters another unit). `disable` therefore shrinks
  the MultiSignal and fixes the trigger faces' offsets behind it, every tool checks
  that the list walks before writing, and `repair-signals UNIT` fixes files made by
  the first version of `disable`.
* A unit's stream ID is `LOAD_HashUnit(name)`. Every time Raziel enters a unit, the
  game reloads that unit's level and `.crm` (`LoadLevels`). A unit needs a bigfile
  directory with its ID, else the game stops with "Could not read directory hash".

```
# a new unit: a copy of under2 called under5, tinted, without its exit to under4
python units.py --disc <path>/KAIN2.img --install <exe folder>/mods new under2 under5 \
    --tint 255 110 110 --disable-portal under4
# make under1's portal lead to under5 instead of under2
python units.py --disc <path>/KAIN2.img --install <exe folder>/mods retarget under1 under2 under5
```

* `new` copies the source unit's level (from the mods folder if it has one, else the
  disc), sets the name and stream ID, updates the monsters' valid-unit lists,
  copies the `.crm` (and `.snf`/`.smf` sound, if any), and adds
  `<new> = <source>` to `mods/units.txt`. The name must be an area name plus a
  number (it sets the path `\kain2\area\<area>\bin\<unit>.drm`) and its ID must not
  collide with an existing unit's.
* For each line of `units.txt` the port (`port/src/mod_files.c`) gives the new unit
  a bigfile directory: a copy of the source unit's directory (its object and sound
  files stay on the disc, so their names needn't be known) plus every mods file,
  and adds it to the directory table the game reads at startup.
* `retarget` changes a unit's portal records and stream signals from one
  destination to another; `disable` removes a portal record and empties its
  signal. Both sides of a connection must agree: `"<B>,<n>"` in A must be the
  number of B's portal back to A, and the other way round.

Tested: `under5`, a red-tinted copy of `under2`, reached from `under1` through its
portal, and loading directly with `-area under5`.

**Moving and adding portals** (`portals.py`). When a neighbour streams in, the game
moves it so that its portal's box minimum lands on the matching portal's box
minimum (`STREAM_ConnectStream`: offset = main.min - neighbour.min, then
`RelocateLevel`). Units are only moved, never turned, so both sides of a
connection need portals of the same size and orientation; only their positions
may differ. Moving or adding the portal on one side is enough: the unit on the
other side follows.

```
# move under1's portal 0 by (dx, dy, dz): record and trigger faces
python units.py --disc <path>/KAIN2.img --install <mods> move-portal under1 0 -4000 0 0
# a second exit from under1 to under6 (arriving at under6's portal 2), shaped like
# under1's portal 0, with its box minimum at (-2500, -2500, 0)
python units.py --disc <path>/KAIN2.img --install <mods> add-portal under1 under6 2 \
    --like under1:0 --at -2500 -2500 0
# and under6's way back must name that new portal (number 2 in under1)
python units.py --disc <path>/KAIN2.img --install <mods> retarget under6 under1 under1 --number 2
```

`add-portal` appends a record to `Terrain.StreamUnits`, a `MultiSignal` with the
stream signal at `Level.SignalListEnd` (offset `0xD4`), and two trigger faces (attr
`0x44`, `textoff` = the signal's offset from `Terrain.signals`) to the tree with ID
-1, then rebuilds the BSP trees. The trigger fires only when crossed from the front
of its triangles to the back (`COLLIDE_LineWithSignals`), so the quad's corner order
sets which way Raziel has to walk through it; `--like` keeps the other portal's
orientation. `--quad` takes four corners A B C D (t1 = ABC, t2 = ACD).

Because units are never turned, a copy can only attach to a wall that faces the
same way as its source's portal. `new ... --rotate 90|180|270` turns the copy in
the data instead (`transform.py`): vertices, morph data, intros (position,
spectral offset, facing) and their position splines, portals, VMObjects; then
it rebuilds every face normal from the winding and recompiles the BSP trees.
Cameras, markers, point and spot lights, the light list's directions and
non-stream signals are not turned (reported when present). `add-portal
--rotate` turns a `--like` shape the same way.

`cut-opening UNIT x0 y0 z0 x1 y1 z1` deletes the walls and ceiling of a tree
(default tree 0) that reach into the box, keeping faces whose normal points
mostly up (the floor), then rebuilds the BSP trees.

Tested: a second exit in `under1`'s west wall (an opening cut with `cut-opening`)
leading to `under6`, a copy of `under2` turned by 180 degrees and tinted blue,
while the original east exit still leads to `under5`:

```
units.py ... new under2 under6 --rotate 180 --tint 110 140 255 --disable-portal under4
units.py ... cut-opening under1 -5100 -800 30 -4200 1800 1700
units.py ... add-portal under1 under6 2 --like under1:0 --rotate 180 --at -4900 -600 0
units.py ... retarget under6 under1 under1 --number 3
```

### A unit built from nothing (newunit.py)

```
python newunit.py --disc <path>/KAIN2.img rewr1 --connect city9 --wall-y 0 \
    --door 10273 -646 10909 1267 --install <exe folder>/mods
```

makes a unit with its own name (here `rewr1`, area `rewr`, a stream ID no other
unit has) and generated content, connected to `city9` through a doorway cut into
one of its south-facing walls. No other unit changes:

* **Container:** the level-file structure (Level and Terrain headers, light
  list, fog, signal list, one portal record) comes from an existing unit's file
  (`--container`, default `city19`) with all of its content removed (vertices,
  faces, morph data, intros, animated objects and textures, event scripts, its
  other portals). The container's own unit keeps its files and is not touched.
* **Room:** a box (floor, ceiling, four walls, about 4000 x 5000 x 3000, tiled
  every 1000 units) facing inwards behind the doorway, textured with the
  connected unit's most used floor and wall textures. Its `.crm` is a copy of the
  connected unit's, so both units' textures agree with the video memory either
  one loads. A Raziel start inside, and BSP trees compiled.
* **Connection:** in the connected unit, the wall faces lying entirely inside
  the doorway are removed (`cut-opening ... inside`: pick a doorway along the
  wall's existing edges so the faces around it stay whole) and a portal to the
  new unit is added; the new unit's portal leads back to it. Both units use the
  same coordinates, so the game lines them up with no offset.

Choose a doorway where the wall has nothing behind it and doesn't change shape in
the spectral realm (vertices without morph entries), else the hole and the
doorway stop lining up there.

Tested: `rewr1`, a brick room behind `city9`'s north wall at x 10273..10909,
reached through the new doorway and loading on its own with `-area rewr1`.

### Object video memory (vrambudget.py)

Each object type gets one block of video memory for its textures, the size in
its `Object.vramSize` (Raziel 64x256, a hunter 40x256, an aluka 44x256 halfwords).
Every type gets a block, not every instance. All the blocks share the object
area, x 512..1023 and y 256..511 (512 columns of 256 lines), with the font and
the objects the engine keeps loaded everywhere (Raziel, glyph icons, particles,
soul and health effects: about 140 columns). The game keeps the types of the
current unit *and of every unit its portals lead to* loaded together. When a block
doesn't fit even after the game compacts the area, the object is used without
one, and its textures show whatever else is in video memory.

So adding enemies to one unit can scramble them in a neighbour. Enemies added to
`city9` take room in `city19`'s group, which also loads `city20`. `levelpatch.py`,
`units.py` and `newunit.py` print the budget of every group an edit touches after
writing, and warn when one is over:

```
object video memory (city20):
  city20    46% of the object area (238 of 512 columns), room for 16 more 16-wide objects
  city19    86% of the object area (439 of 512 columns): OVER, no room for aluka (44x256)
```

A type can bring others with it: a hunter loads `xbow` and `fthrow`, which are
counted as well. A block can't cross a 64-column texture page, so a group can be
over before it reaches 512 columns. Fewer distinct types near each other is what
helps; more instances of a type already there cost nothing.

```
# a unit's group and the groups of its neighbours, with every type listed
python vrambudget.py --disc <path>/KAIN2.img --install <exe folder>/mods -v city19
# the original game's groups, largest first (all of them fit; oracle5 is full)
python vrambudget.py --disc <path>/KAIN2.img --survey under1 city9
```

The simulation is a port of the game's allocator (`VRAM.c`). It doesn't know
about objects spawned during play, or about types still loaded from a unit you
just left. Start the port with `-vramlog` to see the real placements in `vram.log`
next to the executable, including any type that got no block.

### Every object type in the game (objcatalog.py)

```
python objcatalog.py --disc <path>/KAIN2.img --mods <exe folder>/mods --out catalog.json
```

Walks every unit reachable through portals (369 units from `under1` and `city9`)
and lists the 375 object types they place, as JSON: how many of each there are
and in which units, a unit to copy one from (one that loads the type itself and
has an intro of it, for `--intro-templates`), its kind (creature, boss, object,
effect, Raziel's start), its video-memory block, the types it brings along, and
its first model in its rest pose (each segment's vertices plus the pivots of the
segment and its parents; models face -Y). The Blender add-on uses it for its
object library and to draw each object as its model.

Intro type names aren't always spelled the same (`Hunter` in `tower1`, `hunter`
elsewhere); `intros.py` matches them without case.

## Recompiling BSP trees (bspbuild.py)

`bspbuild.py` rebuilds every non-empty BSP tree of a level from its faces:

```
python bspbuild.py --disc <path>/KAIN2.img under1 --install <rewraithed exe folder>/mods
python bspbuild.py --drm edited.drm -o rebuilt.drm --segments 2000
```

How it builds a tree (`bsp.py`):

* It splits recursively until a leaf has at most 16 faces (`--leaf-max`).
  Candidate planes are the planes of a sample of the faces plus axis-aligned
  planes through the median. The score favours balance first, then few faces
  crossing the plane.
* Faces are never cut: a face goes to the side its centroid is on, and the
  node's error fields record how far each child's geometry crosses the plane.
  Collision is therefore correct for any choice of plane; the planes only
  decide how well the tree prunes.
* Errors are computed with the engine's integer plane test, so they're exact
  for the stored, quantised plane. Spheres and boxes use the same code as `levelpatch.py`.
* Nodes and leaves are stored in pre-order, front child first, as in the original data.
  Each tree's faces are reordered within its own slice of `faceList` so every
  leaf's faces are contiguous; `morphNormalIdx` is reordered with them. Nothing
  else in a level points at faces (checked on all 369 units), and the engine
  only refers to faces by index at run time.
* The BSP region is replaced as a whole (`Drm.replace`), so trees may need more
  or fewer nodes than before. Empty trees keep aliasing whatever follows them.

`under1` rebuilt: main tree 242 leaves, depth 8 (original 243 leaves, depth 11).
All 369 units reachable from `under1` recompile and pass the checks below (segment
tests on a sample of 25 units: 0 misses); in total 50,938 leaves against the
original 51,502, maximum depth 11 as in the original data. About 1 s per tree of
2,500 faces.

Before writing, the result goes through the same checks as `levelpatch.py`, plus a
segment test. Random segments (falls through surfaces and short moves across them)
are pushed through a reimplementation of the engine's traversal (`bsp.traverse`:
plane errors with its 5-unit margin, leaf boxes), and every face a segment really
crosses must be in a visited leaf. On the original data this gives 0 misses (one
spectral miss in `city9`, from the original's ±1 spectral boxes). Zeroing the
error fields of a rebuilt tree gives dozens of misses, so the test does catch
broken trees.

In the port, a recompiled `under1` with the same scripted input plays identically to
the original, frame for frame, including Raziel being stopped by a pillar.

## Files

| file | |
|---|---|
| `disc.py` | ISO 9660 reader for the CD image, `BIGFILE.DAT` directories, `LOAD_HashName` / `LOAD_HashUnit` |
| `drm.py` | relocation table (`LOAD_RelocBinaryData` / `RESOLVE_Pointers`), writing back, inserting bytes with pointer fix-up |
| `edit.py` | layout-preserving geometry edits and the rebuild of normals and BSP bounds |
| `levelpatch.py` | command line for edits (OBJ or scripted) |
| `textures.py` | `.crm` in video memory, `TextureFT3` entries, texture page images |
| `newtex.py` | new images: PNG reading, 16-colour reduction, placing into the `.crm` |
| `vramspace.py` | which parts of a unit's video memory are used, and free spots |
| `vrambudget.py` | object video memory: whether every object type near a unit gets its textures |
| `objcatalog.py` | every object type the game places, with a unit to copy it from and its model |
| `transform.py` | turning a whole unit by 90, 180 or 270 degrees |
| `portals.py` | moving, reshaping and adding portals |
| `newunit.py` | a unit built from nothing: emptied container, generated room, doorway into an existing unit |
| `units.py` | portals (retarget, disable) and new units |
| `intros.py` | intros as Blender arrows, and moving, adding and deleting them |
| `geometry.py` | edits that add or delete vertices and faces (used by `levelpatch.py`) |
| `bsp.py` | BSP compiler, tree layout, and the engine's traversal for checking |
| `bspbuild.py` | command line: recompile a level's trees, check, write |
| `level.py` | struct layouts: `Level`, `Terrain`, `TVertex`, `TFace`, `Intro`, `StreamUnitPortal`, `MorphVertex`, `BSPTree`, `BSPNode`, `BSPLeaf` |
| `analyze.py` | BSP walking and the checks of stored bounds against the geometry |
| `objexport.py` | OBJ writer |
| `leveldump.py` | command line |

## Format notes

References are to the soul-re decompilation (`include/Game/TYPES.h`,
`src/Game/LOAD3D.c`, `STREAM.c`, `MEMPACK.c`, `COLLIDE.c`).

### Finding a level on the disc

* `STREAM_FillOutFileNames`: unit `under1` lives at `\kain2\area\under\bin\under1.drm`
  (area = name up to the first digit). `.crm` (VRAM/textures) and `.snf` (sound)
  sit next to it.
* `BIGFILE.DAT` starts with `long numSubDirs` and `numSubDirs` entries of
  `{short streamUnitID; short numFiles; long subDirOffset}`. Entry 0 is the
  root directory; the others are per-area directories whose `streamUnitID` is
  `LOAD_HashUnit(unitName)` (for example `under1` = -9234, which is also
  `Level.streamUnitID`).
* Each directory is `{long numFiles; {long fileHash, fileLen, filePos, checkSum}[]}`.
  Files are found by `LOAD_HashName(path)`. Offsets are bytes from the start of
  `BIGFILE.DAT`; the disc uses Form 1 sectors (2048 bytes of user data).

### Relocation (`.drm`)

```
long numPointers
long offsets[numPointers]
pad to tableSize = ((numPointers + 512) / 512) * 512 words   (a multiple of 2048 bytes)
body
```

Every `offsets[i]` is a byte offset into the body where a 32-bit pointer is
stored; that pointer holds a body offset and gets the load address added.
NULL pointers are 0 and are not in the table. The body starts with the
`Level` struct.

### Terrain

* `normalList` is packed 3-short triples (6 bytes each, not the 8-byte `Normal`
  struct): `x` is an unsigned 13-bit value in the low bits of the first short
  (the engine masks it with `0x1FFF` and never sign-extends), and the top 3 bits
  hold the dominant axis (0 = x, 1 = y, 2 = z). A face's normal is
  `cross(v1 - v0, v2 - v0)` normalised to 4096; `TFace.normal` is the index, and
  a negative index means the negated normal (`COLLIDE_GetNormal`). So every
  stored normal has x >= 0 (all 323,188 in the 369 units do), and faces pointing
  towards -x use a negative index; entry 0 is a (0,0,0) dummy because 0 can't be
  negated. A writer that stores a normal with negative x breaks collision on
  that face: the engine reads it as a large positive x and Raziel falls through.
* `vertexList` holds the material realm positions. `TVertex.rgb15` is the
  material vertex colour (15-bit); `r0,g0,b0` are the same colour at 8 bits.
* `MorphDiffList` is terminated by `vindex < 0`. For each entry,
  `(hx,hy,hz)` equals the material position (89,895 of 90,023 entries over 369
  units; the rest differ slightly) and `(x,y,z)` is the spectral minus material delta
  (`MORPH_UpdateNormals`: spectral = h + delta). Vertices not in the list do not move.
* `MorphColorList` has one 15-bit colour per vertex (the spectral colour),
  `morphNormalIdx` one normal index per face (spectral face normal).
  Both are swapped with the live values when the realm changes.
* `StreamUnits` is `long count` then `count` `StreamUnitPortal`s.
  `tolevelname` is `"<unit>,<signal>"`. The signal matches the `MSignalID` of the
  portal on the other side (`under1` portal `MSignalID=1 -> "under2,2"`; `under2`
  has `MSignalID=2 -> "under1,1"`). `min/max` is the portal box, `t1`/`t2` the two
  triangles of the portal quad. When a unit streams in, it is shifted by
  `other.min - this.min` of the matching portal pair (`STREAM_ConnectStream`).
  Units are not in one shared coordinate space: of 832 matched portal pairs, 276 need
  no shift (for example `under1`/`under2`) and 556 do.
* Double-sided surfaces (for example the `flags 0x2` tree in `under1` and the
  water tree in `city9`, face attr `0x08`) store every triangle twice on the same
  three vertices, with opposite winding. The second copy's `TFace.normal` is
  the negated normal index of the first (as a signed short: 63587 = -1949).
  Blender merges faces that share a vertex set, so the exporter puts the second
  copies into a separate `<tree>_backfaces` object.
* The portal quads also exist as terrain faces in a tree with `ID = -1`
  (face attr `0x44`, sometimes `0x54`).

### BSP trees

* Each `BSPTree` has nodes stored contiguously from `bspRoot` to `startLeaves`
  and leaves from `startLeaves` to `endLeaves`. Nodes have `flags = 0x1` and
  leaves `flags = 0x2`; traversal (`COLLIDE_*`) tells them apart by `flags & 2`.
  A tree with `bspRoot == startLeaves == endLeaves` is empty (its root
  points at the next tree's first leaf).
* Every face belongs to exactly one leaf of exactly one tree; leaf face ranges
  are contiguous slices of `faceList`.
* Vertices are local to the tree that owns their faces: the world position is
  vertex + `globalOffset` (collision and drawing both subtract `globalOffset`
  from the camera or query position; `RelocateLevel` moves `globalOffset`, not the
  vertices). In all 369 units, `globalOffset` and `localOffset` are 0 on disc.
* Tree `flags`: `0x1` not drawn, `0x2` special collision path, `0x40` no light
  instance, `0x100`/`0x4000`/`0x2000` blocks / Raziel-only / monster-only collision
  overrides, `0x1000` related to `ignore_attr & 0x10`. `ID < 0` trees are skipped by
  normal collision and drawing.
* Leaves hold at most 16 faces (51,480 of 51,502 leaves; the largest has 18).
  For `under1`'s main tree: 242 nodes, 243 leaves, leaf depth 7 to 11, about 10
  faces per leaf.

How the stored bounds relate to the geometry (checked on all 369 units reachable
through portals from `under1`, all of which parse):

* `BSPLeaf.box` is the exact AABB of the leaf's vertices in the material realm.
  `BSPLeaf.spectralBox` is the AABB of the spectral positions, to within ±1
  unit (the tool that built them used unrounded spectral positions; the file
  stores rounded deltas). 99.7% of leaves match to ±1; the rest are off by at most 2.
* `sphere` / `spectralSphere` (nodes and leaves) enclose all vertices beneath
  them in that realm, to within rounding (overshoot below 3 units). The radius is
  tight around the stored centre (the farthest vertex is on the surface), but the
  centre is neither the AABB centre, nor the vertex mean, nor the minimal
  enclosing sphere. It matches a Ritter-style "grow from an initial pair"
  sphere for about half the leaves. Any enclosing sphere works for the engine.
* The plane is `(a,b,c)` in 1.3.12 fixed point (|n| ≈ 4096) and `d`, with
  `dist(p) = (a·x + b·y + c·z) >> 12 - d`. `front` is the positive side.
* The four error fields describe how far geometry crosses the splitting plane, per realm:

  ```
  front_X_error = max(0, max over vertices of faces in the BACK child  of dist(p_X))
  back_X_error  = min(0, min over vertices of faces in the FRONT child of dist(p_X))
  ```

  So `front_error ≥ 0` is how far back-side geometry pokes out in front of the
  plane, and `back_error ≤ 0` is how far front-side geometry reaches behind it.
  `COLLIDE_PointAndTerrainFunc` uses them (plus a 5-unit margin) to decide whether a
  segment only needs to visit one child: beyond `front_error` only the front
  child can be hit, below `back_error` only the back child. Faces that straddle a
  splitting plane are kept whole on one side, and the error fields account for them.
  The `material` pair uses material positions, the `spectral` pair spectral positions;
  when the realm changes, the engine swaps the spheres and boxes, while the
  error fields are picked per query by `in_spectral`. The recomputed values match
  to ±3.5 for 95–97% of nodes. The larger differences (up to 26) are on planes far
  from the origin, where the fixed-point normal loses precision: the
  original tool measured distances against the unquantised plane.
