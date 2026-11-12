"""Endian-aware binary reading/writing helpers.

Final Exam assets come in two flavours:
  * PC      -> little-endian, 4-byte tags stored byte-reversed ("OORZ" on disk)
  * PS3     -> big-endian,    plain tags ("ZROO" on disk)

Both are supported by the same code: the reader knows the byte order and
"un-reverses" tags, so the rest of the add-on only ever sees logical tags.
"""

import struct


class Reader:
    def __init__(self, data, endian="<"):
        self.data = data
        self.endian = endian
        self.pos = 0

    # -- primitives ---------------------------------------------------
    def u8(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self):
        v = struct.unpack_from(self.endian + "H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def u32(self):
        v = struct.unpack_from(self.endian + "I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def i32(self):
        v = struct.unpack_from(self.endian + "i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def f32(self):
        v = struct.unpack_from(self.endian + "f", self.data, self.pos)[0]
        self.pos += 4
        return v

    def vec(self, n):
        v = struct.unpack_from(self.endian + "%df" % n, self.data, self.pos)
        self.pos += 4 * n
        return v

    def raw(self, n):
        v = self.data[self.pos:self.pos + n]
        self.pos += n
        return v

    def string(self):
        """Length-prefixed string (no NUL terminator, no padding)."""
        n = self.u32()
        return self.raw(n).decode("latin-1")

    def tag(self):
        raw = self.raw(4)
        if self.endian == "<":
            raw = raw[::-1]
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError:
            return raw.decode("latin-1")

    def peek_tag(self):
        saved = self.pos
        t = self.tag()
        self.pos = saved
        return t

    def eof(self):
        return self.pos >= len(self.data)


class Writer:
    def __init__(self, endian="<"):
        self.endian = endian
        self.buf = bytearray()

    def u8(self, v):
        self.buf.append(v & 0xFF)

    def u16(self, v):
        self.buf += struct.pack(self.endian + "H", v & 0xFFFF)

    def u32(self, v):
        self.buf += struct.pack(self.endian + "I", int(v) & 0xFFFFFFFF)

    def i32(self, v):
        self.buf += struct.pack(self.endian + "i", int(v))

    def f32(self, v):
        self.buf += struct.pack(self.endian + "f", float(v))

    def vec(self, values):
        for v in values:
            self.f32(v)

    def raw(self, data):
        self.buf += data

    def string(self, text):
        raw = text.encode("latin-1", "replace")
        self.u32(len(raw))
        self.raw(raw)

    def tag(self, text):
        raw = text.encode("latin-1")[:4].ljust(4, b"\x00")
        if self.endian == "<":
            raw = raw[::-1]
        self.raw(raw)

    def bytes(self):
        return bytes(self.buf)


def detect_endian(data):
    """Detect the byte order of a ZWO/GEO/ANI file."""
    if len(data) < 12:
        raise ValueError("file too small")
    if data[4:8] == b"OORZ":          # "ZROO" byte-reversed -> little endian
        return "<"
    if data[4:8] == b"ZROO":
        return ">"
    # ZWO uses a 0x24 magic, GEO/ANI use 0x4000; both are 32-bit values.
    le = struct.unpack_from("<I", data, 0)[0]
    be = struct.unpack_from(">I", data, 0)[0]
    if le in (0x24, 0x4000):
        return "<"
    if be in (0x24, 0x4000):
        return ">"
    raise ValueError("not a Final Exam asset (unknown byte order)")


def crc32(data):
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF
