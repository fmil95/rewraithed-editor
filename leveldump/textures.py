"""Terrain textures: the unit's .crm in video memory, TextureFT3 entries, page images.

A unit's .crm is a 36-byte header followed by raw 16-bit video memory, uploaded
by STREAM.c to (Level.vramSize.x + 512, vramSize.y), vramSize.w x vramSize.h
halfwords. TextureFT3 entries (12 bytes, TFace.textoff = byte offset from
Terrain.StartTextureList, 0xFFFF = none) hold, for the face's three corners in
v0, v1, v2 order:
    u0 v0 clut  u1 v1 tpage  u2 v2 attr
* tpage: PlayStation texture page: x = (tpage & 15) * 64 halfwords,
  y = ((tpage >> 4) & 1) * 256, bits 5-6 semi-transparency, bits 7-8 colour
  mode (0 = 4-bit, 1 = 8-bit, 2 = 15-bit). All terrain seen so far is 4-bit.
* clut: palette position, x = (clut & 63) * 16, y = clut >> 6.
* u, v: texel within the 256x256 page.
* attr: gameplay flags read by collision and physics (0x1000, 0x2000, 0x4000,
  0x8000), not drawing.

Blender: each (tpage, clut) pair becomes a material "tex_<tpage>_<clut>" whose
image is the page decoded with that palette (page_<tpage>_<clut>.png, alpha 0
for the transparent colour 0). UVs are u / 256, 1 - v / 256.
"""

import os
import struct
import zlib

CRM_HEADER = 36
VRAM_W, VRAM_H = 1024, 512


def build_vram(level, crm):
    """1024x512 halfwords with the unit's .crm placed where the game puts it."""
    vram = [0] * (VRAM_W * VRAM_H)
    x0, y0, w, h = level.drm.unpack("hhhh", 0x9C)
    x0 += 512
    data = crm[CRM_HEADER:]
    n = min(len(data) // 2, w * h)
    px = struct.unpack("<%dH" % n, data[:n * 2])
    for row in range(h):
        if (row + 1) * w > n:
            break
        base = (y0 + row) * VRAM_W + x0
        vram[base: base + w] = px[row * w: (row + 1) * w]
    return vram


def _rgba(c):
    if c == 0:
        return (0, 0, 0, 0)
    return (((c & 31) * 255) // 31, (((c >> 5) & 31) * 255) // 31, (((c >> 10) & 31) * 255) // 31, 255)


def page_image(vram, tpage, clut):
    """256x256 RGBA bytes of a texture page seen through a palette."""
    tx = (tpage & 15) * 64
    ty = ((tpage >> 4) & 1) * 256
    mode = (tpage >> 7) & 3
    cx, cy = (clut & 63) * 16, clut >> 6
    pal = [_rgba(vram[cy * VRAM_W + cx + i]) if 0 <= cy < VRAM_H and cx + i < VRAM_W else (0, 0, 0, 0)
           for i in range(256)]
    out = bytearray(256 * 256 * 4)
    for v in range(256):
        y = ty + v
        if y >= VRAM_H:
            break
        rowbase = y * VRAM_W
        for u in range(256):
            if mode == 0:
                hw = vram[rowbase + tx + (u >> 2)] if tx + (u >> 2) < VRAM_W else 0
                col = pal[(hw >> ((u & 3) * 4)) & 15]
            elif mode == 1:
                hw = vram[rowbase + tx + (u >> 1)] if tx + (u >> 1) < VRAM_W else 0
                col = pal[(hw >> ((u & 1) * 8)) & 255]
            else:
                col = _rgba(vram[rowbase + tx + u]) if tx + u < VRAM_W else (0, 0, 0, 0)
            o = (v * 256 + u) * 4
            out[o:o + 4] = bytes(col)
    return bytes(out)


def write_png(path, w, h, rgba):
    rows = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, x):
        return struct.pack(">I", len(x)) + t + x + struct.pack(">I", zlib.crc32(t + x) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def texture_entries(level):
    """[(u0, v0, clut, u1, v1, tpage, u2, v2, attr)] from StartTextureList to EndTextureList."""
    t = level.terrain
    st, en = t.ptrs["StartTextureList"], t.ptrs["EndTextureList"]
    if st is None or en is None:
        return []
    return [level.drm.unpack("BBHBBHBBH", st + 12 * i) for i in range((en - st) // 12)]


def face_texture(level, entries, face):
    """(tpage, clut, [(u, v) for v0, v1, v2]) or None for an untextured face."""
    if face.textoff == 0xFFFF or face.textoff % 12 or face.textoff // 12 >= len(entries):
        return None
    e = entries[face.textoff // 12]
    return e[5], e[2], [(e[0], e[1]), (e[3], e[4]), (e[6], e[7])]


def material_name(tpage, clut):
    return "tex_%04x_%04x" % (tpage, clut)


def parse_material(name):
    """(tpage, clut) from "tex_tttt_cccc" (Blender may append ".001")."""
    if not name or not name.startswith("tex_"):
        return None
    parts = name.split(".")[0].split("_")
    try:
        return int(parts[1], 16), int(parts[2], 16)
    except (IndexError, ValueError):
        return None


def write_pages(out_dir, unit, level, crm, used):
    """PNG per (tpage, clut) in `used`, plus the .mtl; returns the .mtl file name."""
    vram = build_vram(level, crm)
    tex_dir = os.path.join(out_dir, "%s_textures" % unit)
    os.makedirs(tex_dir, exist_ok=True)
    mtl = "%s.mtl" % unit
    with open(os.path.join(out_dir, mtl), "w", newline="\n") as f:
        for tpage, clut in sorted(used):
            png = "page_%04x_%04x.png" % (tpage, clut)
            write_png(os.path.join(tex_dir, png), 256, 256, page_image(vram, tpage, clut))
            f.write("newmtl %s\nKd 1 1 1\nmap_Kd %s_textures/%s\nmap_d %s_textures/%s\n\n" % (
                material_name(tpage, clut), unit, png, unit, png))
    return mtl
