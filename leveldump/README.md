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
node above a changed face. Adding or deleting geometry also needs the vertex
and face lists to grow or shrink, which isn't supported yet (the BSP compiler
below is the other half).

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

`--hill X Y RADIUS HEIGHT [--tint R G B]` is a scripted test edit that needs no
Blender. Every output is re-read and checked before it's written. An OBJ
exported by Blender without changes patches back to a byte-identical level.

The port loads `mods\kain2\area\under\bin\under1.drm` next to `rewraithed.exe`
in place of the copy in `BIGFILE.DAT`; delete the file to go back. For quick
tests: `rewraithed.exe -area under1 -skipmenu -skiplogos -skipcutscene`.

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
