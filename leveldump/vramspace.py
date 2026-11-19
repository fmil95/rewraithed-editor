"""Which parts of a unit's video-memory block are in use, and where new images can go.

The block is Level.vramSize (w x h halfwords) at (vramSize.x + 512, vramSize.y).
Used are:
* every texture entry's texels: the bounding box of its three UVs in its page
  (4-bit pages: 4 texels per halfword, 8-bit: 2, 15-bit: 1);
* every palette in use (16 halfwords for 4-bit, 256 for 8-bit);
* animated textures (Terrain.aniList, Level.bgAniList): the destination
  rectangle and palette, and the source rectangle and palette of every frame.

Everything else in the block is free: nothing the level refers to reads it.
"""

import textures

AREA_X = 512


def block_rect(level):
    x, y, w, h = level.drm.unpack("hhhh", 0x9C)
    return x + AREA_X, y, w, h


class UsageMap:
    def __init__(self, level):
        self.level = level
        self.bx, self.by, self.bw, self.bh = block_rect(level)
        self.used = bytearray(self.bw * self.bh)     # 1 = used, per halfword
        self.reasons = {"texels": 0, "palettes": 0, "animated": 0}
        self._mark_textures()
        self._mark_animated(level.terrain.ptrs["aniList"])
        bg = level.drm.ptr(0x84) if level.drm.is_ptr(0x84) else None
        self._mark_animated(bg)

    def mark(self, x, y, w, h, why=None):
        n = 0
        for yy in range(max(y, self.by), min(y + h, self.by + self.bh)):
            row = (yy - self.by) * self.bw
            for xx in range(max(x, self.bx), min(x + w, self.bx + self.bw)):
                if not self.used[row + xx - self.bx]:
                    self.used[row + xx - self.bx] = 1
                    n += 1
        if why:
            self.reasons[why] += n

    def _mark_textures(self):
        for e in textures.texture_entries(self.level):
            u = (e[0], e[3], e[6])
            v = (e[1], e[4], e[7])
            tpage, clut = e[5], e[2]
            mode = (tpage >> 7) & 3
            per = {0: 4, 1: 2}.get(mode, 1)
            px = (tpage & 15) * 64
            py = ((tpage >> 4) & 1) * 256
            x0 = px + min(u) // per
            x1 = px + max(u) // per
            self.mark(x0, py + min(v), x1 - x0 + 1, max(v) - min(v) + 1, "texels")
            self.mark((clut & 63) * 16, clut >> 6, 16 if mode == 0 else 256, 1, "palettes")

    def _mark_animated(self, ani):
        d = self.level.drm
        if ani is None:
            return
        n = d.s32(ani)
        for i in range(n):
            loc = ani + 4 + 4 * i
            if not d.is_ptr(loc):
                continue
            o = d.ptr(loc)
            dx, dy, w, h, cx, cy, cw, ch = d.unpack("8h", o)
            frames = d.s32(o + 0x18)
            self.mark(dx, dy, w, h, "animated")
            self.mark(cx, cy, cw, ch, "animated")
            for f in range(frames):
                sx, sy, scx, scy = d.unpack("4h", o + 0x20 + 8 * f)
                self.mark(sx, sy, w, h, "animated")
                self.mark(scx, scy, cw, ch, "animated")

    def is_free(self, x, y, w, h):
        if x < self.bx or y < self.by or x + w > self.bx + self.bw or y + h > self.by + self.bh:
            return False
        for yy in range(y, y + h):
            row = (yy - self.by) * self.bw
            if any(self.used[row + x - self.bx: row + x - self.bx + w]):
                return False
        return True

    def free_count(self):
        return self.used.count(0)

    def find_image_spot(self, w_hw, h):
        """Free w_hw x h halfwords inside one texture page (pages are 64 halfwords
        wide for 4-bit textures), scanning on a 4-halfword grid. Returns (x, y) or None."""
        for y in range(self.by, self.by + self.bh - h + 1, 4):
            for x in range(self.bx, self.bx + self.bw - w_hw + 1, 4):
                if (x // 64) != ((x + w_hw - 1) // 64) or (y // 256) != ((y + h - 1) // 256):
                    continue          # must stay inside one texture page
                if self.is_free(x, y, w_hw, h):
                    return x, y
        return None

    def find_palette_spot(self, n=16):
        """Free n halfwords at an x that is a multiple of 16 (palette alignment)."""
        for y in range(self.by, self.by + self.bh):
            for x in range(self.bx - self.bx % 16, self.bx + self.bw - n + 1, 16):
                if x >= self.bx and self.is_free(x, y, n, 1):
                    return x, y
        return None
