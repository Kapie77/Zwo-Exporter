"""`.hvt` texture container -> DDS.

Final Exam stores textures in a small cross-platform container:

    " IVH"
    chunk "HEAD"  -> format id ("DXT1"/"DXT5"/"ARGB"), width, height, platform
    chunk "DATA"  -> one per mip level: [index][?][byte length][data]
    chunk "END"

The DXT payload is plain (non-swizzled) little-endian DXT1/DXT5, so it can be
handed to Blender by wrapping it in a normal DDS header.  That keeps the add-on
free of any texture decoder.
"""

import os
import struct
import zlib

BLOCK_BYTES = {"DXT1": 8, "DXT3": 16, "DXT5": 16}
SUPPORTED_FORMATS = set(BLOCK_BYTES) | {"ARGB", "XRGB"}


class HvtTexture:
    def __init__(self):
        self.format = ""
        self.width = 0
        self.height = 0
        self.mips = []          # [(index, bytes)]
        self.source = None
        self.endian = "<"

    @property
    def valid(self):
        return self.format in SUPPORTED_FORMATS and self.width > 0 \
            and self.height > 0 and bool(self.mips)

    def __repr__(self):
        return "<HvtTexture %s %dx%d mips=%d>" % (
            self.format, self.width, self.height, len(self.mips))

    # -- DDS ----------------------------------------------------------
    def build_dds(self):
        """Return a complete .dds file as bytes."""
        if self.format in BLOCK_BYTES:
            return self._build_dds_block()
        if self.format in ("ARGB", "XRGB"):
            return self._build_dds_32bit()
        raise ValueError("unsupported texture (%s %dx%d)" % (
            self.format, self.width, self.height))

    def _build_dds_32bit(self):
        if self.width <= 0 or self.height <= 0 or not self.mips:
            raise ValueError("unsupported texture (%s %dx%d)" % (
                self.format, self.width, self.height))
        mips = sorted(self.mips, key=lambda m: m[0])
        payload = b"".join(data for _idx, data in mips)
        flags = 0x1 | 0x2 | 0x4 | 0x8 | 0x1000 | 0x20000
        header = struct.pack("<7I", 124, flags, self.height, self.width,
                             self.width * 4, 0, max(1, len(mips)))
        header += b"\x00" * 44
        alpha = 0x8 if self.format == "ARGB" else 0x0
        header += struct.pack("<2I", 32, 0x41 | alpha)   # size, flags
        header += struct.pack("<2I", 0, 32)              # fourCC (0 = RGB), bit count
        header += struct.pack("<4I", 0x00FF0000, 0x0000FF00,
                              0x000000FF, 0xFF000000 if alpha else 0)
        header += struct.pack("<5I", 0, 0, 0, 0, 0)
        assert len(header) == 124, len(header)
        return b"DDS " + header + payload

    def _build_dds_block(self):
        block = BLOCK_BYTES[self.format]
        mips = sorted(self.mips, key=lambda m: m[0])
        # Drop mips that are obviously wrong (truncated container).
        payload = b"".join(data for _idx, data in mips)
        linear = max(1, (self.width + 3) // 4) * max(1, (self.height + 3) // 4) * block
        fourcc = self.format.encode("ascii")
        flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000
        header = struct.pack("<7I", 124, flags, self.height, self.width,
                             linear, 0, max(1, len(mips)))
        header += b"\x00" * 44
        header += struct.pack("<2I", 32, 0x4) + fourcc + b"\x00" * 20
        header += struct.pack("<4I", 0x401008, 0, 0, 0)
        header += b"\x00" * 4
        assert len(header) == 124, len(header)
        return b"DDS " + header + payload

    def save_dds(self, directory, name=None):
        if name is None:
            key = "%s|%d|%d|%s" % (self.source or self.format, self.width, self.height, self.format)
            name = "tex_%08x" % (zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name + ".dds")
        data = self.build_dds()
        # Rewrite only when the content changed.
        try:
            with open(path, "rb") as fh:
                if fh.read() == data:
                    return path
        except OSError:
            pass
        with open(path, "wb") as fh:
            fh.write(data)
        return path


def _u32(data, offset, endian):
    if offset + 4 > len(data):
        return 0
    return struct.unpack_from(endian + "I", data, offset)[0]


def load_hvt(path, endian=None):
    with open(path, "rb") as fh:
        return parse_hvt(fh.read(), endian, path)


def _detect_hvt_endian(data):
    if data.startswith(b"HVI "):
        return "<"            # PC: byte-reversed tags, little-endian payload
    if data.startswith(b" IVH"):
        return ">"            # PS3: plain tags, big-endian payload
    return "<"


def parse_hvt(data, endian=None, source=None):
    """Parse a `.hvt` container.

    On PC every tag is stored byte reversed (``HVI ``, ``DAEH``, ``ATAD``); on
    PS3 the tags read normally and the numbers are big-endian.  The mip chunks
    carry their own size, so the stream is walked rather than scanned.
    """
    if endian is None:
        endian = _detect_hvt_endian(data)
    tag = (lambda name: name[::-1].encode("ascii")) if endian == "<" \
        else (lambda name: name.encode("ascii"))

    tex = HvtTexture()
    tex.source = source
    if not data.startswith((b" IVH", b"HVI ")):
        raise ValueError("not an HVT file")
    head = data.find(tag("HEAD"))
    if head < 0:
        raise ValueError("HVT without HEAD")
    tex.endian = endian

    fmt = data[head + 8:head + 12]
    if endian == "<":
        fmt = fmt[::-1]
    try:
        tex.format = fmt.decode("ascii").strip("\x00")
    except UnicodeDecodeError:
        tex.format = ""
    width = _u32(data, head + 12, endian)
    height = _u32(data, head + 16, endian)
    if 1 <= width <= 8192 and 1 <= height <= 8192:
        tex.width, tex.height = width, height
    if tex.format not in BLOCK_BYTES:
        for candidate in (b"DXT1", b"DXT5", b"DXT3"):
            if candidate in data[:256] or candidate[::-1] in data[:256]:
                tex.format = candidate.decode("ascii")
                break

    # Mip chunks: [u32 chunk size][DATA][u32 index][u32 ?][u32 length][payload]
    marker = tag("DATA")
    pos = head + 20
    while True:
        at = data.find(marker, pos)
        if at < 0 or at < 4:
            break
        size = _u32(data, at - 4, endian)
        if size < 20 or at - 4 + size > len(data):
            break
        index = _u32(data, at + 4, endian)
        length = _u32(data, at + 12, endian)
        payload = at + 16
        if length <= size - 20 and index < 32:
            tex.mips.append((index, data[payload:payload + length]))
        pos = at - 4 + size
    if len(tex.mips) > 1:
        seen = {}
        for index, blob in tex.mips:
            seen.setdefault(index, blob)
        tex.mips = sorted(seen.items())
    return tex


def texture_path_for_asset(asset_path, asset_root):
    """Map a `.geo` texture reference to a `.hvt` file on disk.

    Material properties store bare names (``facade_diff``), so the bare name is
    looked up in a per-root index of every ``.hvt`` in the asset tree.
    """
    if not asset_path:
        return None
    cleaned = asset_path.replace("\\", "/").strip("/")
    candidates = []
    if cleaned.lower().endswith(".hvt"):
        candidates.append(cleaned)
        stem = cleaned[:-4]
    else:
        candidates.append(cleaned + ".hvt")
        stem = cleaned
    for rel in candidates:
        if asset_root:
            direct = os.path.join(asset_root, *rel.split("/"))
            if os.path.isfile(direct):
                return direct
    return resolve_texture(stem, asset_root)


_TEXTURE_INDEX = {}


def build_texture_index(asset_root, force=False):
    """Index every `.hvt` below ``asset_root`` by lower-case file stem."""
    key = os.path.abspath(asset_root) if asset_root else ""
    if not force and key in _TEXTURE_INDEX:
        return _TEXTURE_INDEX[key]
    index = {}
    for folder, _dirs, files in os.walk(asset_root):
        for name in files:
            if not name.lower().endswith(".hvt"):
                continue
            stem = name[:-4].lower()
            index.setdefault(stem, os.path.join(folder, name))
    _TEXTURE_INDEX[key] = index
    return index


def resolve_texture(name, asset_root):
    """Return the `.hvt` path whose stem matches ``name`` (case insensitive)."""
    if not name or not asset_root:
        return None
    stem = name.replace("\\", "/").rsplit("/", 1)[-1]
    if stem.lower().endswith(".hvt"):
        stem = stem[:-4]
    return build_texture_index(asset_root).get(stem.lower())
