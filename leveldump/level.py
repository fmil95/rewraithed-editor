"""Parse a relocated level .drm into Python objects.

Layouts follow soul-re/include/Game/TYPES.h (Level, Terrain, TVertex, TFace,
Normal, Intro, StreamUnitPortal, MorphVertex, MorphColor, BSPTree, BSPNode,
BSPLeaf). All pointers are body offsets (see drm.Drm).
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from drm import Drm

# --- sizes -----------------------------------------------------------------
SIZEOF_TVERTEX = 0xC
SIZEOF_TFACE = 0xC
# Terrain normals are packed 3-short triples (COLLIDE_GetNormal), not the
# 8-byte Normal struct: x is an UNSIGNED 13-bit value in the low bits of the
# first short (the engine masks with 0x1FFF and never sign-extends), and the
# top 3 bits hold the dominant axis (0 = x, 1 = y, 2 = z). So every stored
# normal has x >= 0; a face facing the other way uses the negative index.
# Index 0 is a (0,0,0) dummy, since it can't be negated.
SIZEOF_NORMAL = 0x6
SIZEOF_INTRO = 0x4C
SIZEOF_PORTAL = 0x5C
SIZEOF_MORPHVERTEX = 0xE
SIZEOF_BSPTREE = 0x24
SIZEOF_BSPNODE = 0x2C
SIZEOF_BSPLEAF = 0x30
SIZEOF_TEXTUREFT3 = 0xC

BSP_FLAG_LEAF = 0x2  # BSPNode.flags / BSPLeaf.flags bit tested by COLLIDE_*


@dataclass
class Sphere:
    x: int
    y: int
    z: int
    radius: int

    @classmethod
    def read(cls, d: Drm, off):
        x, y, z, r = d.unpack("hhhH", off)
        return cls(x, y, z, r)


@dataclass
class Box:
    minx: int
    miny: int
    minz: int
    maxx: int
    maxy: int
    maxz: int

    @classmethod
    def read(cls, d: Drm, off):
        return cls(*d.unpack("6h", off))


@dataclass
class Vertex:
    x: int
    y: int
    z: int
    rgb15: int
    r0: int
    g0: int
    b0: int
    code: int


@dataclass
class Face:
    v0: int
    v1: int
    v2: int
    attr: int
    sortPush: int
    normal: int
    textoff: int


@dataclass
class Intro:
    offset: int
    name: str
    intronum: int
    UniqueID: int
    rotation: Tuple[int, int, int]
    position: Tuple[int, int, int]
    maxRad: int
    flags: int
    has_data: bool
    has_multiSpline: bool
    specturalLightGroup: int
    meshColor: int
    spectralPosition: Tuple[int, int, int]


@dataclass
class Portal:
    tolevelname: str
    MSignalID: int
    streamID: int
    flags: int
    min: Tuple[int, int, int]
    max: Tuple[int, int, int]
    t1: List[Tuple[int, int, int]]
    t2: List[Tuple[int, int, int]]

    @property
    def dest_unit(self):
        return self.tolevelname.split(",")[0]

    @property
    def dest_signal(self):
        parts = self.tolevelname.split(",", 1)
        try:
            return int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            return None


@dataclass
class MorphVertex:
    x: int  # spectral - material delta
    y: int
    z: int
    vindex: int
    hx: int  # material position
    hy: int
    hz: int


@dataclass
class BSPNode:
    offset: int
    index: int
    sphere: Sphere
    a: int
    b: int
    c: int
    flags: int
    d: int
    front: Optional[int]
    back: Optional[int]
    spectralSphere: Sphere
    front_spectral_error: int
    back_spectral_error: int
    front_material_error: int
    back_material_error: int


@dataclass
class BSPLeaf:
    offset: int
    index: int
    sphere: Sphere
    faceList: Optional[int]  # body offset
    first_face: Optional[int]  # index into Terrain.faces
    numFaces: int
    flags: int
    box: Box
    spectralBox: Box
    spectralSphere: Sphere


@dataclass
class BSPTree:
    index: int
    bspRoot: Optional[int]
    startLeaves: Optional[int]
    endLeaves: Optional[int]
    globalOffset: Tuple[int, int, int]
    flags: int
    localOffset: Tuple[int, int, int]
    ID: int
    splineID: int
    nodes: List[BSPNode] = field(default_factory=list)
    leaves: List[BSPLeaf] = field(default_factory=list)
    node_at: dict = field(default_factory=dict)
    leaf_at: dict = field(default_factory=dict)


@dataclass
class Terrain:
    offset: int
    UnitChangeFlags: int
    numIntros: int
    numVertices: int
    numFaces: int
    numNormals: int
    numBSPTrees: int
    ptrs: dict
    vertices: List[Vertex] = field(default_factory=list)
    faces: List[Face] = field(default_factory=list)
    normals: List[Tuple[int, int, int, int]] = field(default_factory=list)  # (x, y, z, axis)
    intros: List[Intro] = field(default_factory=list)
    portals: List[Portal] = field(default_factory=list)
    morph_vertices: List[MorphVertex] = field(default_factory=list)
    morph_colors: List[int] = field(default_factory=list)
    morph_normals: List[int] = field(default_factory=list)
    trees: List[BSPTree] = field(default_factory=list)


@dataclass
class Level:
    drm: Drm
    worldName: str
    streamUnitID: int
    versionNumber: int
    numIntros: int
    numBSPLeaves: int
    bspPlaneError: int
    waterZLevel: int
    deathZ: int
    flags: int
    unitFlags: int
    fog: dict
    terrain: Terrain


TERRAIN_PTRS = {
    "introList": 0x0C, "vertexList": 0x1C, "faceList": 0x20, "normalList": 0x24,
    "aniList": 0x28, "StreamUnits": 0x30, "StartTextureList": 0x34,
    "EndTextureList": 0x38, "MorphDiffList": 0x3C, "MorphColorList": 0x40,
    "BSPTreeArray": 0x48, "morphNormalIdx": 0x4C, "signals": 0x50,
}


def decode_normal(sx, y, z):
    return (sx & 0x1FFF, y, z, (sx >> 13) & 7)


def encode_normal(x, y, z, axis):
    if not 0 <= x <= 0x1FFF:
        raise ValueError("stored normals need 0 <= x <= 8191, got %d" % x)
    v = x | ((axis & 7) << 13)
    if v & 0x8000:
        v -= 0x10000
    return (v, y, z)


def signed16(v):
    return v - 0x10000 if v & 0x8000 else v


def _str16(d: Drm, off):
    return d.body[off:off + 16].split(b"\0")[0].decode("latin-1")


def parse_terrain(d: Drm, off) -> Terrain:
    t = Terrain(
        offset=off,
        UnitChangeFlags=d.s16(off),
        numIntros=d.s32(off + 0x08),
        numVertices=d.s32(off + 0x10),
        numFaces=d.s32(off + 0x14),
        numNormals=d.s32(off + 0x18),
        numBSPTrees=d.s32(off + 0x44),
        ptrs={k: d.ptr(off + v) for k, v in TERRAIN_PTRS.items()},
    )
    p = t.ptrs

    base = p["vertexList"]
    for i in range(t.numVertices):
        t.vertices.append(Vertex(*d.unpack("hhhHBBBB", base + i * SIZEOF_TVERTEX)))

    base = p["faceList"]
    for i in range(t.numFaces):
        t.faces.append(Face(*d.unpack("HHHBbHH", base + i * SIZEOF_TFACE)))

    if p["normalList"] is not None:
        base = p["normalList"]
        for i in range(t.numNormals):
            t.normals.append(decode_normal(*d.unpack("hhh", base + i * SIZEOF_NORMAL)))

    if p["introList"] is not None:
        base = p["introList"]
        for i in range(t.numIntros):
            o = base + i * SIZEOF_INTRO
            t.intros.append(Intro(
                offset=o,
                name=_str16(d, o),
                intronum=d.s32(o + 0x10),
                UniqueID=d.s32(o + 0x14),
                rotation=d.unpack("hhh", o + 0x18),
                position=d.unpack("hhh", o + 0x20),
                maxRad=d.s16(o + 0x26),
                flags=d.s32(o + 0x2C),
                has_data=d.is_ptr(o + 0x30),
                has_multiSpline=d.is_ptr(o + 0x38),
                specturalLightGroup=d.s16(o + 0x40),
                meshColor=d.s16(o + 0x42),
                spectralPosition=d.unpack("hhh", o + 0x44),
            ))

    if p["StreamUnits"] is not None:
        base = p["StreamUnits"]
        n = d.s32(base)
        for i in range(n):
            o = base + 4 + i * SIZEOF_PORTAL
            vals = d.unpack("hhhh", o + 0x18)
            mx = d.unpack("hhhh", o + 0x20)
            t.portals.append(Portal(
                tolevelname=_str16(d, o),
                MSignalID=d.s32(o + 0x10),
                streamID=d.s32(o + 0x14),
                flags=vals[3],
                min=vals[:3],
                max=mx[:3],
                t1=[d.unpack("hhh", o + 0x2C + k * 8) for k in range(3)],
                t2=[d.unpack("hhh", o + 0x44 + k * 8) for k in range(3)],
            ))

    if p["MorphDiffList"] is not None:
        o = p["MorphDiffList"]
        while True:
            mv = MorphVertex(*d.unpack("7h", o))
            if mv.vindex < 0:
                break
            t.morph_vertices.append(mv)
            o += SIZEOF_MORPHVERTEX

    if p["MorphColorList"] is not None:
        t.morph_colors = list(d.unpack("%dH" % t.numVertices, p["MorphColorList"]))

    if p["morphNormalIdx"] is not None:
        t.morph_normals = list(d.unpack("%dH" % t.numFaces, p["morphNormalIdx"]))

    if p["BSPTreeArray"] is not None:
        for i in range(t.numBSPTrees):
            t.trees.append(parse_tree(d, t, p["BSPTreeArray"] + i * SIZEOF_BSPTREE, i))
    return t


def parse_tree(d: Drm, t: Terrain, o, index) -> BSPTree:
    tree = BSPTree(
        index=index,
        bspRoot=d.ptr(o + 0x00),
        startLeaves=d.ptr(o + 0x04),
        endLeaves=d.ptr(o + 0x08),
        globalOffset=d.unpack("hhh", o + 0x0C),
        flags=d.s16(o + 0x12),
        localOffset=d.unpack("hhh", o + 0x14),
        ID=d.s16(o + 0x1A),
        splineID=d.s32(o + 0x1C),
    )
    face_base = t.ptrs["faceList"]
    # Nodes are stored contiguously from bspRoot up to startLeaves, leaves from
    # startLeaves to endLeaves (MORPH_UpdateNormals / RelocateBSPTree loops).
    if tree.bspRoot is not None and tree.startLeaves is not None:
        n = (tree.startLeaves - tree.bspRoot) // SIZEOF_BSPNODE
        for i in range(n):
            no = tree.bspRoot + i * SIZEOF_BSPNODE
            a, b, c, flags = d.unpack("hhhh", no + 0x08)
            node = BSPNode(
                offset=no, index=i,
                sphere=Sphere.read(d, no),
                a=a, b=b, c=c, flags=flags,
                d=d.s32(no + 0x10),
                front=d.ptr(no + 0x14),
                back=d.ptr(no + 0x18),
                spectralSphere=Sphere.read(d, no + 0x1C),
                front_spectral_error=d.s16(no + 0x24),
                back_spectral_error=d.s16(no + 0x26),
                front_material_error=d.s16(no + 0x28),
                back_material_error=d.s16(no + 0x2A),
            )
            tree.nodes.append(node)
            tree.node_at[no] = node
    if tree.startLeaves is not None and tree.endLeaves is not None:
        n = (tree.endLeaves - tree.startLeaves) // SIZEOF_BSPLEAF
        for i in range(n):
            lo = tree.startLeaves + i * SIZEOF_BSPLEAF
            fl = d.ptr(lo + 0x08) if d.is_ptr(lo + 0x08) else None
            first = None
            if fl is not None:
                rel = fl - face_base
                if rel % SIZEOF_TFACE:
                    raise ValueError("leaf face pointer not aligned to TFace")
                first = rel // SIZEOF_TFACE
            leaf = BSPLeaf(
                offset=lo, index=i,
                sphere=Sphere.read(d, lo),
                faceList=fl, first_face=first,
                numFaces=d.s16(lo + 0x0C),
                flags=d.s16(lo + 0x0E),
                box=Box.read(d, lo + 0x10),
                spectralBox=Box.read(d, lo + 0x1C),
                spectralSphere=Sphere.read(d, lo + 0x28),
            )
            tree.leaves.append(leaf)
            tree.leaf_at[lo] = leaf
    return tree


def parse_level(data: bytes) -> Level:
    d = Drm(data)
    world = d.ptr(0x98)
    return Level(
        drm=d,
        worldName=d.cstr(world) if world is not None else "",
        streamUnitID=d.s32(0xF8),
        versionNumber=d.unpack("I", 0xF0)[0],
        numIntros=d.s32(0x78),
        numBSPLeaves=d.s32(0xD8),
        bspPlaneError=d.s32(0x60),
        waterZLevel=d.s32(0x38),
        deathZ=d.s32(0x6C),
        flags=d.s32(0x70),
        unitFlags=d.s32(0xCC),
        fog={
            "fogFar": d.u16(0x44), "fogNear": d.u16(0x46),
            "spectralFogFar": d.u16(0x48), "spectralFogNear": d.u16(0x4A),
            "waterFogFar": d.u16(0x4C), "waterFogNear": d.u16(0x4E),
            "holdFogFar": d.u16(0xA4), "holdFogNear": d.u16(0xA6),
        },
        terrain=parse_terrain(d, d.ptr(0x00)),
    )
