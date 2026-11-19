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

Here the body is kept as bytes and pointers stay body offsets; `ptr()` returns
None for a field that is not in the relocation table (i.e. NULL).
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
        self.reloc = set(struct.unpack_from("<%di" % num, data, 4))
        self.body = bytes(data[self.header_size:])
        bad = [r for r in self.reloc if r < 0 or r + 4 > len(self.body) or r & 3]
        if bad:
            raise ValueError("relocation entries outside body: %r" % bad[:5])

    def __len__(self):
        return len(self.body)

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
