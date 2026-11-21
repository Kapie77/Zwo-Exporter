"""`.hvt` texture container -> DDS.

Final Exam stores textures in a small cross-platform container:

    " IVH"
    chunk "HEAD"  -> format id ("DXT1"/"DXT5"/"ARGB"), width, height, platform
    chunk "DATA"  -> one per mip level: [index][?][byte length][data]
    chunk "END"

On PC and PS3 the DXT payload is plain (non-swizzled) DXT1/DXT5, so it can be
handed to Blender by wrapping it in a normal DDS header.  Xbox 360 instead keeps
every level in one Xenos-tiled blob; it is untile'd (and its 16-bit word swap
undone) before the same DDS wrapper is applied, so the add-on still needs no
texture decoder.
"""

import os
import struct
import zlib

BLOCK_BYTES = {"DXT1": 8, "DXT3": 16, "DXT5": 16}
SUPPORTED_FORMATS = set(BLOCK_BYTES) | {"ARGB", "XRGB"}

# Xbox 360: (block width, block height, bytes per block, log2 bytes per block)
_X360_BLOCKS = {
    "DXT1": (4, 4, 8, 3),
    "DXT3": (4, 4, 16, 4),
    "DXT5": (4, 4, 16, 4),
    "ARGB": (1, 1, 4, 2),
    "XRGB": (1, 1, 4, 2),
}
_X360_TILE = 32                # blocks per tile edge (128 texels for DXT)
_X360_SUBRESOURCE_ALIGN = 4096


class HvtTexture:
    def __init__(self):
        self.format = ""
        self.width = 0
        self.height = 0
        self.mips = []          # [(index, bytes)]
        self.source = None
        self.endian = "<"
        self.platform = ""
        self.array_size = 1

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


# ------------------------------------------------------------- Xbox 360 tiling
# The X360 GPU stores textures in a 32x32-block tiled layout ("Tiled2D" in
# Xenia) with the mip chain following the base level and the smallest levels
# packed together into a single tile.  The payload is also byte-swapped in
# 16-bit words relative to the PS3 DXT blocks.

def _align(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def _next_pow2(value):
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def _log2_ceil(value):
    if value <= 1:
        return 0
    return (value - 1).bit_length()


def _swap16(data):
    if len(data) < 2:
        return data
    out = bytearray(data)
    out[0::2], out[1::2] = data[1::2], data[0::2]
    return bytes(out)


def _reverse_argb(data):
    """X360 32-bit pixels are stored ``A,R,G,B``; the DDS builder wants ``B,G,R,A``."""
    if len(data) < 4:
        return data
    out = bytearray(len(data))
    for i in range(0, len(data) - 3, 4):
        out[i:i + 4] = data[i + 3:i + 4] + data[i + 2:i + 3] \
            + data[i + 1:i + 2] + data[i:i + 1]
    return bytes(out)


def _x360_tiled2d(x, y, pitch_aligned, bpb_log2):
    """Byte offset of block (x, y) inside a 32x32-block tile grid."""
    outer = ((y >> 5) * (pitch_aligned >> 5) + (x >> 5)) << 6
    inner = (((y >> 1) & 7) << 3) | (x & 7)
    oib = (outer | inner) << bpb_log2
    bank = (y >> 4) & 1
    pipe = ((x >> 3) & 3) ^ (((y >> 3) & 1) << 1)
    return (((y & 1) << 4) | (pipe << 6) | (bank << 11) | (oib & 0xF)
            | (((oib >> 4) & 1) << 5) | (((oib >> 5) & 7) << 8)
            | ((oib >> 8) << 12))


def _x360_packed_level(width, height):
    size = _log2_ceil(min(width, height))
    return size - 4 if size > 4 else 0


def _x360_packed_offset(width, height, level, block_w, block_h):
    log_w, log_h = _log2_ceil(width), _log2_ceil(height)
    size = min(log_w, log_h)
    base = size - 4 if size > 4 else 0
    shift = level - base
    if shift < 3:
        if log_w > log_h:
            x, y = 0, 16 >> shift
        else:
            x, y = 16 >> shift, 0
    else:
        if log_w > log_h:
            x, y = (1 << (log_w - base)) >> (shift - 2), 0
        else:
            x, y = 0, (1 << (log_h - base)) >> (shift - 2)
    return x // block_w, y // block_h


def _x360_pitch_blocks(width, level, block_w, base_pitch):
    texels = base_pitch if level == 0 else max(_next_pow2(width) >> level, 1)
    return _align(_align(texels, block_w) // block_w, _X360_TILE)


def _x360_height_blocks(height, level, block_h):
    texels = height if level == 0 else max(_next_pow2(height) >> level, 1)
    return _align(_align(texels, block_h) // block_h, _X360_TILE)


def _decode_x360_mips(tex, payload, mip_count):
    """Turn the single tiled X360 `DATA` blob into linear mip levels.

    Only the first face of a cube map is kept, matching how the PS3 container
    (which repeats the ``0..n`` indices once per face) is collapsed by
    :func:`parse_hvt`.
    """
    spec = _X360_BLOCKS.get(tex.format)
    if spec is None:
        raise ValueError("unsupported X360 texture format %r" % tex.format)
    block_w, block_h, block_bytes, bpb_log2 = spec
    width, height = tex.width, tex.height
    max_levels = max(1, max(width, height).bit_length())
    if not 1 <= mip_count <= max_levels:
        mip_count = max_levels
    if block_bytes >= 8:
        payload = _swap16(payload)
    else:
        payload = _reverse_argb(payload)

    packed = _x360_packed_level(width, height)
    base_pitch = _align(width, _X360_TILE)
    pitches = {}
    sizes = {}
    for level in range(mip_count):
        pitch = _x360_pitch_blocks(width, level, block_w, base_pitch)
        height_blocks = _x360_height_blocks(height, level, block_h)
        pitches[level] = pitch
        sizes[level] = _align(pitch * block_bytes * height_blocks,
                              _X360_SUBRESOURCE_ALIGN)

    separate = min(packed, mip_count)
    per_face = sum(sizes[level] for level in range(separate))
    if mip_count > packed:
        per_face += sizes[packed]
    array_size = 1
    if per_face > 0 and len(payload) >= per_face:
        array_size = max(1, len(payload) // per_face)

    level_start = {0: 0}
    for level in range(1, min(packed, mip_count - 1) + 1):
        level_start[level] = level_start[level - 1] + array_size * sizes[level - 1]

    mips = []
    for level in range(mip_count):
        if level >= packed:
            offset_x, offset_y = _x360_packed_offset(
                width, height, level, block_w, block_h)
            pitch = pitches[packed]
            start = level_start.get(packed, 0)
        else:
            offset_x = offset_y = 0
            pitch = pitches[level]
            start = level_start[level]
        blocks_w = max(1, ((width >> level) + block_w - 1) // block_w)
        blocks_h = max(1, ((height >> level) + block_h - 1) // block_h)
        out = bytearray(blocks_w * blocks_h * block_bytes)
        for by in range(blocks_h):
            row = by + offset_y
            base = by * blocks_w * block_bytes
            for bx in range(blocks_w):
                src = start + _x360_tiled2d(bx + offset_x, row, pitch, bpb_log2)
                dst = base + bx * block_bytes
                if src + block_bytes <= len(payload):
                    out[dst:dst + block_bytes] = payload[src:src + block_bytes]
        mips.append((level, bytes(out)))
    tex.mips = mips
    tex.array_size = array_size
    return mips


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

    platform = data[head + 24:head + 28]
    try:
        tex.platform = platform.decode("ascii").strip("\x00")
    except UnicodeDecodeError:
        tex.platform = ""
    mip_count = _u32(data, head + 28, endian)

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
    if tex.platform.upper().startswith("X360") and tex.mips:
        # The X360 container keeps every level (and every cube face) in a
        # single tiled DATA chunk instead of one chunk per mip.
        blob = max(tex.mips, key=lambda item: len(item[1]))[1]
        _decode_x360_mips(tex, blob, mip_count)
        return tex
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
