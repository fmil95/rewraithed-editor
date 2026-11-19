"""New texture images: PNG in, 4-bit texture + palette placed in the unit's .crm.

An image (any 8-bit PNG: grey, RGB, RGBA or paletted) is shrunk if needed to
fit a free rectangle of the unit's video-memory block (vramspace.py), reduced
to 16 colours by median cut, and written as a 4-bit texture (4 texels per
halfword, first texel in the low bits) inside one texture page, with its
16-colour palette in a free palette slot. Colour 0x0000 is see-through on the
PlayStation: transparent pixels (alpha < 128) get it, and opaque black is
written as the darkest non-zero colour instead.
"""

import struct
import zlib

import textures
import vramspace


# --- PNG ---------------------------------------------------------------------

def read_png(path):
    """(width, height, [(r, g, b, a)] row-major) for 8-bit non-interlaced PNGs."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s is not a PNG" % path)
    pos = 8
    idat = b""
    plte = trns = None
    w = h = depth = ctype = interlace = None
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            plte = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    if depth != 8 or interlace:
        raise ValueError("%s: only 8-bit, non-interlaced PNGs are supported" % path)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    stride = w * channels
    prev = bytearray(stride)
    pixels = []
    p = 0
    for _ in range(h):
        ftype = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 255
            elif ftype == 2:
                line[i] = (line[i] + b) & 255
            elif ftype == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ftype == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        prev = line
        for x in range(w):
            px = line[x * channels:(x + 1) * channels]
            if ctype == 0:
                pixels.append((px[0], px[0], px[0], 255))
            elif ctype == 2:
                pixels.append((px[0], px[1], px[2], 255))
            elif ctype == 3:
                r, g, b = plte[px[0]]
                alpha = trns[px[0]] if trns is not None and px[0] < len(trns) else 255
                pixels.append((r, g, b, alpha))
            elif ctype == 4:
                pixels.append((px[0], px[0], px[0], px[1]))
            else:
                pixels.append((px[0], px[1], px[2], px[3]))
    return w, h, pixels


def resize(w, h, pixels, nw, nh):
    """Box-filtered resize (averages the source pixels behind each new one)."""
    out = []
    for y in range(nh):
        y0, y1 = y * h // nh, max((y + 1) * h // nh, y * h // nh + 1)
        for x in range(nw):
            x0, x1 = x * w // nw, max((x + 1) * w // nw, x * w // nw + 1)
            acc = [0, 0, 0, 0]
            n = 0
            for yy in range(y0, y1):
                for xx in range(x0, x1):
                    p = pixels[yy * w + xx]
                    for k in range(4):
                        acc[k] += p[k]
                    n += 1
            out.append(tuple(v // n for v in acc))
    return out


# --- colour reduction --------------------------------------------------------

def median_cut(colors, n):
    """Up to n representative colours for a list of (r, g, b).

    Works on the distinct colours weighted by how often they occur: an image
    with n colours or fewer keeps them exactly, and a box is split where its
    weight halves *between* two distinct values, so a big flat area can't drag
    a small, different colour into its average."""
    counts = {}
    for c in colors:
        counts[c] = counts.get(c, 0) + 1
    if len(counts) <= n:
        return list(counts)
    boxes = [list(counts.items())]
    while len(boxes) < n:
        boxes.sort(key=lambda b: -_spread(b)[0])
        box = boxes[0]
        spread, axis = _spread(box)
        if spread == 0 or len(box) < 2:
            break
        box.sort(key=lambda cw: cw[0][axis])
        total = sum(w for _, w in box)
        acc, cut = 0, 1
        for i, (_, w) in enumerate(box[:-1]):
            acc += w
            cut = i + 1
            if acc * 2 >= total:
                break
        boxes[0:1] = [box[:cut], box[cut:]]
    out = []
    for b in boxes:
        tw = sum(w for _, w in b)
        out.append(tuple(sum(c[k] * w for c, w in b) // tw for k in range(3)))
    return out


def _spread(box):
    best = (0, 0)
    for k in range(3):
        vals = [c[k] for c, _ in box]
        s = max(vals) - min(vals) if vals else 0
        if s > best[0]:
            best = (s, k)
    return best


def to_15bit(r, g, b):
    c = (r >> 3) | ((g >> 3) << 5) | ((b >> 3) << 10)
    return c if c else 0x0421      # 0x0000 would be see-through


# --- placing images ------------------------------------------------------------

class ImagePlacer:
    """Places new images into a unit's video-memory block and returns texture info.

    The game reloads video memory from a unit's .crm whenever Raziel enters
    that unit (STREAM_MoveIntoNewStreamUnit -> LoadLevels ->
    STREAM_LoadMainVram), and draws the neighbouring units it can see through
    portals from that same memory; that's why neighbouring .crm files agree on
    each other's texels. So an image for unit U must go where U, U's
    neighbours and *their* neighbours use nothing (`watch`), and be written
    into U's and each neighbour's .crm (`neighbours`), so U still looks right
    while a neighbour's .crm is the loaded one."""

    MAX_TEXELS = 128

    def __init__(self, level, crm, neighbours=(), watch=()):
        """`neighbours`: [(unit, level, crm)] written too; `watch`: [level] only
        kept free."""
        self.level = level
        self.crm_header = crm[:textures.CRM_HEADER]
        self.vram = textures.build_vram(level, crm)
        self.usage = vramspace.UsageMap(level)
        self.others = {}            # unit -> [level, crm header, vram]
        for unit, lv, c in neighbours:
            self.others[unit] = [lv, c[:textures.CRM_HEADER], textures.build_vram(lv, c)]
            self._merge(vramspace.UsageMap(lv))
        for lv in watch:
            self._merge(vramspace.UsageMap(lv))
        self.placed = {}            # image path -> info dict
        self.changed = False

    def _merge(self, other):
        if (other.bx, other.by, other.bw, other.bh) != (self.usage.bx, self.usage.by, self.usage.bw, self.usage.bh):
            raise SystemExit("a neighbouring unit uses a different video-memory block")
        u = self.usage.used
        for i, v in enumerate(other.used):
            if v:
                u[i] = 1

    def place(self, path):
        """{tpage, clut, u0, v0, w, h} for the image at `path` (placed once)."""
        if path in self.placed:
            return self.placed[path]
        w, h, pixels = read_png(path)
        spot = None
        size = None
        # largest power-of-two-ish size up to MAX_TEXELS that fits, keeping the aspect
        scale = min(1.0, self.MAX_TEXELS / max(w, h))
        tw, th = max(4, int(w * scale)), max(1, int(h * scale))
        while tw >= 4 and th >= 1:
            tw4 = (tw + 3) // 4 * 4
            spot = self.usage.find_image_spot(tw4 // 4, th)
            if spot:
                size = (tw4, th)
                break
            tw, th = tw * 3 // 4, th * 3 // 4
        if spot is None:
            raise SystemExit("no free video memory left in this unit for %s" % path)
        tw, th = size
        self.usage.mark(spot[0], spot[1], tw // 4, th)     # before looking for the palette's slot
        pal_spot = self.usage.find_palette_spot(16)
        if pal_spot is None:
            raise SystemExit("no free palette slot left in this unit for %s" % path)
        img = resize(w, h, pixels, tw, th)
        transparent = any(p[3] < 128 for p in img)
        opaque = [p[:3] for p in img if p[3] >= 128]
        pal = median_cut(opaque or [(0, 0, 0)], 15 if transparent else 16)
        palette = ([0] if transparent else []) + [to_15bit(*c) for c in pal]
        palette += [palette[-1]] * (16 - len(palette))
        first = 1 if transparent else 0

        def index(p):
            if p[3] < 128:
                return 0
            best = min(range(len(pal)), key=lambda i: sum((pal[i][k] - p[k]) ** 2 for k in range(3)))
            return best + first

        x, y = spot
        px, py = pal_spot
        for vram in [self.vram] + [o[2] for o in self.others.values()]:
            for row in range(th):
                for col in range(0, tw, 4):
                    hw = 0
                    for k in range(4):
                        hw |= index(img[row * tw + col + k]) << (4 * k)
                    vram[(y + row) * textures.VRAM_W + x + col // 4] = hw
            for i in range(16):
                vram[py * textures.VRAM_W + px + i] = palette[i]
        self.usage.mark(x, y, tw // 4, th)
        self.usage.mark(px, py, 16, 1)
        page_x = x // 64 * 64
        info = {
            "tpage": (x // 64) | ((y // 256) << 4),           # 4-bit mode, no blending
            "clut": (px // 16) | (py << 6),
            "u0": (x - page_x) * 4, "v0": y % 256, "w": tw, "h": th,
            "source": (w, h), "vram": (x, y), "palette": (px, py),
        }
        self.placed[path] = info
        self.changed = True
        return info

    def crm_bytes(self, unit=None):
        """The patched .crm of the edited unit, or of neighbour `unit`."""
        level, header, vram = (self.level, self.crm_header, self.vram) if unit is None else self.others[unit]
        bx, by, bw, bh = vramspace.block_rect(level)
        out = bytearray(header)
        for row in range(bh):
            base = (by + row) * textures.VRAM_W + bx
            out += struct.pack("<%dH" % bw, *vram[base: base + bw])
        return bytes(out)


def neighbourhood(bigfile, level, unit):
    """([(unit, level, crm)] of the unit's portal neighbours, [level] of their
    other neighbours), read from the bigfile."""
    from level import parse_level
    import disc as discmod

    def load(u):
        lv = parse_level(bigfile.read_level(u))
        crm = bigfile.read_file(discmod.level_path(u).replace(".drm", ".crm"), u)
        return lv, crm

    names = []
    for p in level.terrain.portals:
        n = p.dest_unit.lower()
        if n and n != "warpgate" and n != unit and n not in names:
            names.append(n)
    neighbours, watch, seen = [], [], {unit} | set(names)
    for n in names:
        try:
            lv, crm = load(n)
        except FileNotFoundError:
            continue
        neighbours.append((n, lv, crm))
        for p in lv.terrain.portals:
            m = p.dest_unit.lower()
            if m and m != "warpgate" and m not in seen:
                seen.add(m)
                try:
                    watch.append(parse_level(bigfile.read_level(m)))
                except FileNotFoundError:
                    pass
    return neighbours, watch


def read_mtl(path):
    """{material name: image path or None} from a .mtl (map_Kd)."""
    import os
    mats = {}
    cur = None
    base = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("newmtl "):
                cur = line[7:].strip()
                mats[cur] = None
            elif line.startswith("map_Kd ") and cur is not None:
                parts = line[7:].split()
                # skip options like "-s 1 1 1" or "-bm 1"
                i = 0
                while i < len(parts) and parts[i].startswith("-"):
                    i += 1 + {"-s": 3, "-o": 3, "-t": 3, "-bm": 1, "-mm": 2, "-blendu": 1,
                              "-blendv": 1, "-clamp": 1, "-imfchan": 1, "-boost": 1}.get(parts[i], 0)
                img = " ".join(parts[i:])
                if img:
                    p = img if os.path.isabs(img) else os.path.join(base, img)
                    mats[cur] = os.path.normpath(p)
    return mats
