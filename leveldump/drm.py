"""Crystal Dynamics relocatable memory image (.drm) as loaded by LOAD_RelocBinaryData.

    long numPointers
    long pointerOffsets[numPointers]
    ... padding up to tableSize words, tableSize = ((numPointers + 512) / 512) * 512
    body (the actual data, e.g. a Level)

Every entry of pointerOffsets is a byte offset into the body at which a 32-bit
pointer is stored; that pointer itself holds a byte offset into the body. The
loader adds the body's load address to each such pointer and then moves the
body down over the table. Words that are not in the table are not pointers:
a NULL pointer is simply 0 and not listed.

Here the body is kept as a bytearray and pointers stay body offsets; `ptr()`
returns None for a field that is not in the relocation table (i.e. NULL).
`to_bytes()` writes the file back; `insert()` grows the body and fixes every
pointer and relocation entry behind the insertion point.
"""

import struct


class Drm:
    def __init__(self, data):
        num = struct.unpack_from("<i", data, 0)[0]
        if not 0 <= num < len(data) // 4:
            raise ValueError("bad relocation count %d" % num)
        self.num_pointers = num
        self.table_words = ((num + 512) // 512) * 512
        self.header_size = self.table_words * 4
        self.reloc_order = list(struct.unpack_from("<%di" % num, data, 4))
        self.reloc = set(self.reloc_order)
        self.body = bytearray(data[self.header_size:])
        bad = [r for r in self.reloc if r < 0 or r + 4 > len(self.body) or r & 3]
        if bad:
            raise ValueError("relocation entries outside body: %r" % bad[:5])

    def __len__(self):
        return len(self.body)

    # --- reading ------------------------------------------------------------

    def is_ptr(self, off):
        return off in self.reloc

    def ptr(self, off):
        """Value of the pointer stored at body offset `off`, or None if NULL."""
        if off not in self.reloc:
            v = struct.unpack_from("<I", self.body, off)[0]
            if v != 0:
                raise ValueError("word at 0x%x = 0x%x is not relocated but nonzero" % (off, v))
            return None
        return struct.unpack_from("<I", self.body, off)[0]

    def unpack(self, fmt, off):
        return struct.unpack_from("<" + fmt, self.body, off)

    def s16(self, off):
        return struct.unpack_from("<h", self.body, off)[0]

    def u16(self, off):
        return struct.unpack_from("<H", self.body, off)[0]

    def s32(self, off):
        return struct.unpack_from("<i", self.body, off)[0]

    def cstr(self, off, maxlen=256):
        end = self.body.find(b"\0", off, off + maxlen)
        if end < 0:
            end = off + maxlen
        return self.body[off:end].decode("latin-1")

    # --- writing ------------------------------------------------------------

    def pack(self, fmt, off, *values):
        struct.pack_into("<" + fmt, self.body, off, *values)

    def insert(self, at, nbytes, fill=b"\0", keep=()):
        """Insert `nbytes` (a multiple of 4) at body offset `at`.

        Pointers whose value is >= `at` move with the data behind them, and so
        do relocation entries stored at or behind `at`. Anything that refers to
        body offsets without being in the relocation table is not adjusted, so
        insert only at the end of a block.
        """
        if nbytes % 4:
            raise ValueError("insertions must keep 4-byte alignment")
        if nbytes == 0:
            return
        keep = set(keep)
        for r in self.reloc_order:
            v = struct.unpack_from("<I", self.body, r)[0]
            # pointers in `keep` stay: inserting into an empty list, its own
            # pointer equals the next block's start but must stay at the new data
            if v >= at and not (r in keep and v == at):
                struct.pack_into("<I", self.body, r, v + nbytes)
        self.body[at:at] = (fill * nbytes)[:nbytes]
        self.reloc_order = [r + nbytes if r >= at else r for r in self.reloc_order]
        self.reloc = set(self.reloc_order)

    def replace(self, start, end, data, pointers=(), allow_into=(), keep=()):
        """Replace body[start:end] with `data` (a multiple of 4 bytes long).

        Relocation entries inside the old range are dropped; `pointers` are the
        offsets (relative to `start`) of the pointers inside `data`, whose
        values the caller writes afterwards. Pointers elsewhere whose value is
        >= `end` move with the data behind the range. A pointer elsewhere that
        points strictly inside the old range would be left dangling, so it is
        an error unless its location is listed in `allow_into` (the caller
        rewrites it).
        """
        delta = len(data) - (end - start)
        if delta % 4 or (end - start) % 4:
            raise ValueError("replacements must keep 4-byte alignment")
        allow = set(allow_into)
        keep = set(keep)          # pointers whose value `start` stays (an empty list's own pointer)
        kept = []
        for r in self.reloc_order:
            if start <= r < end:
                continue
            v = struct.unpack_from("<I", self.body, r)[0]
            if r in keep and v == start:
                pass
            elif v >= end:
                struct.pack_into("<I", self.body, r, v + delta)
            elif start < v < end and r not in allow:
                raise ValueError("pointer at 0x%x points into the replaced range (0x%x)" % (r, v))
            kept.append(r + delta if r >= end else r)
        self.body[start:end] = data
        kept += [start + p for p in pointers]
        self.reloc_order = sorted(kept)
        self.reloc = set(self.reloc_order)
        return delta

    def to_bytes(self):
        num = len(self.reloc_order)
        table_words = ((num + 512) // 512) * 512
        head = bytearray(table_words * 4)
        struct.pack_into("<i", head, 0, num)
        struct.pack_into("<%di" % num, head, 4, *self.reloc_order)
        return bytes(head) + bytes(self.body)
