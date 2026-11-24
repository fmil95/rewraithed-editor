"""Read files out of the game's CD image and its BIGFILE.DAT.

The disc is a single MODE2/2352 data track. Only Form 1 sectors (2048 bytes of
user data) are used for the ISO 9660 filesystem and for BIGFILE.DAT.

BIGFILE.DAT layout (see LOAD_InitCdLoader / LOAD_ReadDirectory in LOAD3D.c):

    long numSubDirs
    BigFileDirEntry subDirList[numSubDirs]   { short streamUnitID; short numFiles; long subDirOffset; }
    ...
    at subDirList[i].subDirOffset:
    BigFileDir { long numFiles; BigFileEntry fileList[numFiles]; }
    BigFileEntry { long fileHash; long fileLen; long filePos; long checkSumFull; }

subDirList[0] is the root directory, searched last by LOAD_GetBigFileEntryByHash;
the other entries are per-area directories keyed by LOAD_HashUnit(unitName).
File positions and directory offsets are byte offsets from the start of BIGFILE.DAT.
"""

import os
import struct

RAW_SECTOR = 2352
USER_DATA = 2048


class CdImage:
    """Raw (2352 bytes/sector) or cooked (2048 bytes/sector) ISO image."""

    def __init__(self, path):
        self.f = open(path, "rb")
        size = os.path.getsize(path)
        self.f.seek(0)
        sync = self.f.read(12)
        if sync == b"\x00" + b"\xff" * 10 + b"\x00":
            self.sector_size = RAW_SECTOR
            mode = self.f.read(4)[3]
            # MODE2 Form1: 12 sync + 4 header + 8 subheader; MODE1: 12 + 4
            self.data_offset = 24 if mode == 2 else 16
        else:
            self.sector_size = USER_DATA
            self.data_offset = 0
        self.num_sectors = size // self.sector_size

    def read_sectors(self, lba, count):
        if self.sector_size == USER_DATA:
            self.f.seek(lba * USER_DATA)
            return self.f.read(count * USER_DATA)
        out = bytearray()
        for i in range(count):
            self.f.seek((lba + i) * RAW_SECTOR + self.data_offset)
            out += self.f.read(USER_DATA)
        return bytes(out)

    def read(self, lba, offset, length):
        """Read `length` bytes starting `offset` bytes into the file that starts at `lba`."""
        first = lba + offset // USER_DATA
        skip = offset % USER_DATA
        count = (skip + length + USER_DATA - 1) // USER_DATA
        return self.read_sectors(first, count)[skip:skip + length]

    # --- ISO 9660 ---------------------------------------------------------

    def _dir_records(self, lba, size):
        data = self.read_sectors(lba, (size + USER_DATA - 1) // USER_DATA)
        pos = 0
        while pos < size:
            rec_len = data[pos]
            if rec_len == 0:
                pos = (pos // USER_DATA + 1) * USER_DATA
                continue
            rec = data[pos:pos + rec_len]
            ext_lba = struct.unpack_from("<I", rec, 2)[0]
            ext_size = struct.unpack_from("<I", rec, 10)[0]
            flags = rec[25]
            name_len = rec[32]
            name = rec[33:33 + name_len].decode("ascii", "replace")
            yield name, ext_lba, ext_size, bool(flags & 2)
            pos += rec_len

    def find(self, path):
        """Return (lba, size) of an ISO 9660 file, e.g. 'BIGFILE.DAT' or 'DIR\\FILE'."""
        pvd = self.read_sectors(16, 1)
        if pvd[1:6] != b"CD001":
            raise ValueError("no ISO 9660 primary volume descriptor")
        root = pvd[156:156 + 34]
        lba = struct.unpack_from("<I", root, 2)[0]
        size = struct.unpack_from("<I", root, 10)[0]
        parts = [p for p in path.replace("/", "\\").split("\\") if p]
        for i, part in enumerate(parts):
            want = part.upper()
            for name, ext_lba, ext_size, is_dir in self._dir_records(lba, size):
                if name.split(";")[0].upper() == want:
                    lba, size = ext_lba, ext_size
                    break
            else:
                raise FileNotFoundError(path)
        return lba, size


# --- hashes (LOAD3D.c) -----------------------------------------------------

HASH_EXTENSIONS = ["drm", "crm", "tim", "smp", "snd", "smf", "snf"]


def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def hash_name(name):
    """LOAD_HashName: hash of a full bigfile path like \\kain2\\area\\under\\bin\\under1.drm."""
    s = name
    strl = len(s) - 1
    ext = 0
    dot = s.find(".")
    if dot >= 0:
        e = s[dot + 1:].lower()
        if e in HASH_EXTENSIONS:
            ext = HASH_EXTENSIONS.index(e)
            strl -= 4
    total = xor = length = 0
    while strl >= 0:
        c = s[strl]
        strl -= 1
        if c == "\\":
            continue
        c = ord(c.upper()) - 0x1A
        # c is a signed char in the original; letters/digits stay positive
        if c > 127:
            c -= 256
        total += c
        xor ^= c * length
        length += 1
    return _s32((length << 27) | (total << 15) | (xor << 3) | ext)


def hash_unit(name):
    """LOAD_HashUnit: 16-bit id of a stream unit name, used as sub directory id."""
    last = h = num = flag = 0
    for ch in name:
        if ch.isdigit():
            num = num * 10 + (ord(ch) - 48)
        else:
            val = ord(ch) - (65 if "A" <= ch <= "Z" else 97)
            h <<= 2
            h += ((val - last) << 5) if flag else (val - last)
            flag ^= 1
            last = val
    h += num
    h &= 0xFFFF
    return h - 0x10000 if h & 0x8000 else h


def level_path(unit):
    """STREAM_FillOutFileNames: \\kain2\\area\\<area>\\bin\\<unit>.drm"""
    area = unit
    for i, ch in enumerate(unit):
        if ch.isdigit():
            area = unit[:i]
            break
    return "\\kain2\\area\\%s\\bin\\%s.drm" % (area, unit)


# A mods folder whose files are read in place of the bigfile's (as the port
# does), for every BigFile that isn't given its own: set by --from-mods, so
# that an edit starts from the unit as it is now in the game.
OVERLAY = None

# More folders laid out the same way, read after the mods folder and before
# the bigfile, in this order: a prototype's maps (proto\<date>\areas, as the
# port's launcher extracts them) and the mods folder under its own edits
# (--layer). As the port serves them when that prototype is loaded.
LAYERS = []


def read_layers(path):
    """The file at game path `path` from the first LAYERS folder that has it, or None."""
    for folder in LAYERS:
        p = os.path.join(folder, *path.strip("\\").split("\\"))
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
    return None


class BigFile:
    def __init__(self, image_path, name="BIGFILE.DAT", mods=None, layers=True):
        self.mods = mods
        self.layers = layers      # False: only `mods` and the bigfile (not LAYERS)
        self.cd = CdImage(image_path)
        self.lba, self.size = self.cd.find(name)
        num = struct.unpack("<i", self.read(0, 4))[0]
        raw = self.read(4, num * 8)
        self.subdirs = [struct.unpack_from("<hhi", raw, i * 8) for i in range(num)]
        self._dirs = {}

    def read(self, offset, length):
        return self.cd.read(self.lba, offset, length)

    def directory(self, index):
        if index not in self._dirs:
            _, num_files, off = self.subdirs[index]
            raw = self.read(off, 4 + num_files * 16)
            n = struct.unpack_from("<i", raw, 0)[0]
            self._dirs[index] = [struct.unpack_from("<iiiI", raw, 4 + i * 16) for i in range(n)]
        return self._dirs[index]

    def lookup(self, path, unit=None):
        """Find a file the way LOAD_GetBigFileEntryByHash does: unit dir first, then root."""
        h = hash_name(path)
        order = []
        if unit is not None:
            uid = hash_unit(unit)
            order += [i for i, d in enumerate(self.subdirs) if i > 0 and d[0] == uid]
        order.append(0)
        for i in order:
            for fh, flen, fpos, chk in self.directory(i):
                if fh == h:
                    return fpos, flen
        # fall back: some builds keep files in a different directory
        for i in range(len(self.subdirs)):
            for fh, flen, fpos, chk in self.directory(i):
                if fh == h:
                    return fpos, flen
        raise FileNotFoundError("%s (hash %08x) not in bigfile" % (path, h & 0xFFFFFFFF))

    def read_file(self, path, unit=None):
        mods = self.mods if self.mods is not None else OVERLAY
        if mods:
            p = os.path.join(mods, *path.strip("\\").split("\\"))
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return f.read()
        data = read_layers(path) if self.layers else None
        if data is not None:
            return data
        pos, length = self.lookup(path, unit)
        return self.read(pos, length)

    def read_level(self, unit):
        return self.read_file(level_path(unit), unit)
