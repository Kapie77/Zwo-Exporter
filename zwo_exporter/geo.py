"""Parser/serialiser for Final Exam `.geo` geometry files.

File layout (big-endian on PS3, little-endian on PC):

    u32      0x4000
    u32      1
    u32      path_length
    byte[]   path
    root[]   one or more GMES / GSKI mesh roots, with other data in between

Each mesh root:

    tag       GMES (static) or GSKI (skinned)
    u32      payload_size
    u32      face_count          } duplicated from the first ZFBU block
    u32      first_block_id      }
    u32      first_block_crc     }
    u32      first_block_size    }
    block[]  vertex / face / draw-call blocks
    byte[]   tail               (bounds, name record, materials or bone names)

A block is:

    u32      id
    u32      checksum
    u32      payload_size
    tag      block tag
    tag      duplicate tag
    byte[]   payload

Known blocks:
    VBFL  vertex buffer     [u32 format][u32 format2][u32 count][count * vertex]
    ZFBU  face buffer       [u32 face_count][face_count * 3 * u16][u32 0]

The vertex record is described by :func:`vertex_layout`.  On top of the
common prefix ``[x][y][A][B][C][color][u,v]`` every record ends with the ``z``
coordinate:

    stride 32  PS3 static      [x][y][A][B][C][c][uv][z]
    stride 48  PS3 skinned     [x][y][A][B][C][c][uv][w0][w1][w2][bones][z]
    stride 52  PS3 skinned x4  [x][y][A][B][C][c][uv][w0..w3][bones][z]
    stride 56  PC static       [x][y][A3][B3][C3][c][uv][z]
    stride 72  PC skinned      [x][y][A3][B3][C3][c][uv][w0][w1][w2][bones][z]

``A``/``B``/``C`` are an orthonormal frame (11-11-10 packed on PS3, three
floats each on PC); they are tangents, not the surface normal.  ``bones`` is
four ``u8`` indices into the ZBON list (file order) and pairs with the weights
in the same order.  ``color`` is ``0xAARRGGBB`` (``0xFFFFFFFF`` = opaque white)
and ``uv`` is two IEEE half floats packed into one word.
"""

import struct

from .binio import Reader, Writer, detect_endian

MESH_TAGS = ("GMES", "GSKI")
BLOCK_TAGS = (
    "VBFL", "ZFBU", "ZSEQ", "ZMAT", "ZSKN", "ZSKA", "ZBON", "ZAND",
    "XUTF", "PVBF", "LVBF", "DZSE", "QZSE", "ESZD", "ERTX",
)


def _printable(raw):
    return bool(raw) and all(32 <= byte < 127 for byte in raw)


def _tag_bytes(name, endian):
    """Return the four bytes a tag has on disk for this byte order."""
    raw = name.encode("latin-1")[:4].ljust(4, b"\x00")
    return raw[::-1] if endian == "<" else raw


def _tag_name(raw, endian):
    if endian == "<":
        raw = raw[::-1]
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return raw.decode("latin-1")

# Vertex formats seen in the shipped assets (little/big endian agnostic).
FMT_STATIC32 = 0x000F0005
FMT_SKINNED48 = 0x000F1805
FMT_SKINNED48B = 0x001F1805
FMT_POSITION12 = 0x00000001
FMT_FLAGS4 = 0x00100000


class GeoBlock:
    __slots__ = ("id", "checksum", "tag", "payload", "offset")

    def __init__(self, block_id=0, checksum=0, tag="", payload=b"", offset=0):
        self.id = block_id
        self.checksum = checksum
        self.tag = tag
        self.payload = payload
        self.offset = offset

    def __repr__(self):
        return "<GeoBlock %s id=%d len=%d>" % (self.tag, self.id, len(self.payload))


class GeoMaterial:
    """One ZMAT material record from the `.geo` material palette."""

    def __init__(self):
        self.name = ""
        self.path = ""
        self.size = 0
        self.properties = {}
        self.raw = b""

    def get(self, key, default=None):
        return self.properties.get(key.lower(), default)

    @property
    def diffuse_color(self):
        v = self.get("diffusecolor")
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            return (v[0], v[1], v[2], 1.0)
        return (0.8, 0.8, 0.8, 1.0)

    @property
    def texture_path(self):
        return self.get("diffusetexture") or self.get("texture") or ""

    def __repr__(self):
        return "<GeoMaterial %r tex=%r>" % (self.name, self.texture_path)


class GeoMesh:
    def __init__(self):
        self.tag = "GMES"
        self.index = -1
        # decoded geometry
        self.positions = []
        self.normals = []
        self.uvs = []
        self.colors = []            # RGBA floats
        self.bone_indices = []      # 4 ints per vertex
        self.bone_weights = []      # 4 floats per vertex
        self.faces = []
        self.stride = 0
        self.format_id = 0
        self.format_id2 = 0
        self.vertex_raw = []        # one bytes object per vertex (stride long)
        # preserved structure
        self.blocks = []
        self.blocks_raw = b""      # exact bytes between the face count and the tail
        self.tail = b""
        self.head_ints = [0, 0, 0, 0]
        self.payload_size = 0
        self.material = None
        self.material_path = ""
        self.model_path = ""        # name/path string stored before the record
        self.bone_names = []
        self.bounds = None
        self.needs_rebuild = False

    # -- decoded helpers ---------------------------------------------
    @property
    def skinned(self):
        return self.stride >= 48

    @property
    def vertex_count(self):
        return len(self.positions)

    def __repr__(self):
        return "<GeoMesh %s v=%d f=%d stride=%d mat=%r>" % (
            self.tag, len(self.positions), len(self.faces), self.stride,
            self.material.name if self.material else None)


class GeoFile:
    def __init__(self):
        self.endian = ">"
        self.magic = 0x4000
        self.version = 1
        self.path = ""
        self.items = []             # ("mesh", GeoMesh) / ("raw", bytes)
        self.materials = []
        self.source = None
        self.opaque = None          # set for unparsable stub files

    # -- accessors ---------------------------------------------------
    @property
    def meshes(self):
        return [item[1] for item in self.items if item[0] == "mesh"]

    # -- parsing -----------------------------------------------------
    @classmethod
    def load(cls, path):
        with open(path, "rb") as fh:
            data = fh.read()
        obj = cls.parse(data)
        obj.source = path
        return obj

    @classmethod
    def parse(cls, data):
        endian = detect_endian(data)
        obj = cls()
        obj.endian = endian
        r = Reader(data, endian)
        obj.magic = r.u32()
        obj.version = r.u32()
        if obj.magic != 0x4000:
            raise ValueError("not a GEO file (magic=%#x)" % obj.magic)
        path_len = struct.unpack_from(endian + "I", data, 8)[0]
        if path_len > len(data) - 12:
            # Some shipped files are 24-byte stubs that repeat the magic; keep
            # them untouched instead of inventing a structure.
            obj.opaque = data
            return obj
        obj.path = r.string()

        roots = _find_roots(data, endian)
        cursor = r.pos
        mesh_no = 0
        for off, size in roots:
            if off < cursor:
                continue
            if off > cursor:
                obj.items.append(("raw", data[cursor:off]))
            mesh = _parse_mesh(data, endian, off, size)
            mesh.index = mesh_no
            mesh.model_path = _preceding_string(data, endian, off) or \
                (obj.path if mesh_no == 0 else "")
            mesh_no += 1
            obj.items.append(("mesh", mesh))
            cursor = off + 8 + size
        if cursor < len(data):
            obj.items.append(("raw", data[cursor:]))

        obj.materials = _parse_material_palette(data, endian, roots)
        _assign_materials(obj)
        return obj

    # -- writing -----------------------------------------------------
    def save(self, path):
        with open(path, "wb") as fh:
            fh.write(self.serialize())

    def serialize(self):
        if self.opaque is not None:
            return self.opaque
        w = Writer(self.endian)
        w.u32(0x4000)
        w.u32(self.version)
        w.string(self.path)
        for kind, payload in self.items:
            if kind == "raw":
                w.raw(payload)
            else:
                _write_mesh(w, payload)
        return w.bytes()


# ---------------------------------------------------------------- parsing
def _find_roots(data, endian):
    """Return [(offset, payload_size)] for every valid GMES/GSKI root."""
    out = []
    n = len(data)
    tags = {_tag_bytes(name, endian): name for name in MESH_TAGS}
    i = 0
    while i < n - 12:
        tag = data[i:i + 4]
        if tag in tags:
            size = struct.unpack_from(endian + "I", data, i + 4)[0]
            if 0 < size <= n - i - 8 and _valid_root(data, endian, i, size):
                out.append((i, size))
                i += 8 + size
                continue
        i += 1
    return out


def _valid_root(data, endian, off, size):
    p = off + 8 + 4          # skip payload header ints
    if p + 20 > len(data):
        return False
    length = struct.unpack_from(endian + "I", data, p + 8)[0]
    tag, tag2 = data[p + 12:p + 16], data[p + 16:p + 20]
    return tag == tag2 and _tag_name(tag, endian) in BLOCK_TAGS \
        and 0 <= length <= len(data) - (p + 20)


def _parse_blocks(data, endian, pos, end):
    blocks = []
    guard = 0
    while pos + 20 <= end and guard < 100000:
        guard += 1
        length = struct.unpack_from(endian + "I", data, pos + 8)[0]
        tag, tag2 = data[pos + 12:pos + 16], data[pos + 16:pos + 20]
        name = _tag_name(tag, endian) if tag == tag2 else None
        if name not in BLOCK_TAGS:
            alt = pos - 4
            alt_tag = data[alt + 12:alt + 16] if alt >= 0 else b""
            alt_name = _tag_name(alt_tag, endian) if alt_tag else ""
            if alt >= 0 and data[alt + 12:alt + 16] == data[alt + 16:alt + 20] \
                    and alt_name in BLOCK_TAGS:
                pos = alt
                length = struct.unpack_from(endian + "I", data, pos + 8)[0]
                tag = alt_tag
                name = alt_name
            else:
                break
        payload_at = pos + 20
        if length > len(data) - payload_at or payload_at + length > end:
            break
        blocks.append(GeoBlock(
            struct.unpack_from(endian + "I", data, pos)[0],
            struct.unpack_from(endian + "I", data, pos + 4)[0],
            name,
            data[payload_at:payload_at + length],
            pos,
        ))
        pos = payload_at + length
    return blocks, pos


def _parse_mesh(data, endian, off, size):
    mesh = GeoMesh()
    mesh.tag = _tag_name(data[off:off + 4], endian)
    payload = off + 8
    mesh.head_ints = list(struct.unpack_from(endian + "4I", data, payload))
    blocks, chain_end = _parse_blocks(data, endian, payload + 4, min(payload + size, len(data)))
    mesh.blocks = blocks
    mesh.blocks_raw = data[payload + 4:chain_end]
    mesh.payload_size = size
    mesh.tail = data[chain_end:payload + size]

    main_vb = None
    face_payload = None
    for block in blocks:
        if block.tag == "VBFL" and len(block.payload) >= 16:
            fmt, fmt2, count = struct.unpack_from(endian + "3I", block.payload, 0)
            if count and (len(block.payload) - 16) % count == 0:
                stride = (len(block.payload) - 16) // count
                if main_vb is None or stride > main_vb[2]:
                    main_vb = (block, fmt, stride, count, fmt2)
        elif block.tag == "ZFBU" and len(block.payload) >= 8:
            if face_payload is None:
                face_payload = block

    if main_vb is not None:
        block, fmt, stride, count, fmt2 = main_vb
        mesh.format_id, mesh.format_id2, mesh.stride = fmt, fmt2, stride
        if stride >= 12:
            _decode_vertices(mesh, endian, block.payload, stride, count)
            if not mesh.faces and face_payload is not None:
                _decode_faces(mesh, endian, face_payload.payload)

    # A skinned mesh lists its bone names in the root tail.
    mesh.bone_names = _tail_strings(mesh.tail, endian)
    return mesh


def vertex_layout(endian, stride):
    """Return the field offsets for one vertex of the given stride.

    Verified against both the PC and the PS3 builds of the shipped assets.

    Both layouts share a common shape::

        [x][y][z][A][B][C][color][u,v] [skin ...]

    ``A``/``B``/``C`` are an orthonormal frame, stored as three 11-11-10 packed
    words on PS3 and expanded to nine floats on PC.  The 4-byte preamble of a
    ``VBFL`` block (format, format2, count, then the first vertex) means the
    vertex data starts at payload + 12, not at payload + 16.
    """
    pc = endian == "<"
    base = 56 if pc else 32
    layout = {
        "pc": pc,
        "x": 0,
        "y": 4,
        "z": 8,
        "frame": ("floats", 12) if pc else ("packed", 12),
        "color": 48 if pc else 24,
        "uv": 52 if pc else 28,
        "weights": [],
        "bones": None,
    }
    extra = stride - base
    if extra >= 12:
        nweights = (extra - 4) // 4
        start = base
        layout["weights"] = [start + 4 * k for k in range(nweights)]
        layout["bones"] = start + 4 * nweights
    return layout


def _decode_vertices(mesh, endian, payload, stride, count):
    base = payload[12:]
    if len(base) < stride * count:
        count = len(base) // stride
    lay = vertex_layout(endian, stride)
    pos = mesh.positions
    nrm = mesh.normals
    uvs = mesh.uvs
    col = mesh.colors
    raw = mesh.vertex_raw
    for i in range(count):
        o = i * stride
        chunk = bytes(base[o:o + stride])
        raw.append(chunk)
        x = struct.unpack_from(endian + "f", chunk, lay["x"])[0]
        y = struct.unpack_from(endian + "f", chunk, lay["y"])[0]
        z = struct.unpack_from(endian + "f", chunk, lay["z"])[0]
        pos.append((x, y, z))
        nrm.append(_frame_normal(chunk, lay))
        if stride >= 28:
            uword = struct.unpack_from(endian + "I", chunk, lay["uv"])[0]
            uvs.append((_half(uword & 0xFFFF), _half(uword >> 16)))
            c = struct.unpack_from(endian + "I", chunk, lay["color"])[0]
            if c == 0xFFFFFFFF:
                col.append((1.0, 1.0, 1.0, 1.0))
            else:
                col.append(((c >> 16 & 255) / 255.0, (c >> 8 & 255) / 255.0,
                            (c & 255) / 255.0, (c >> 24 & 255) / 255.0))
        else:
            uvs.append((0.0, 0.0))
            col.append((1.0, 1.0, 1.0, 1.0))
        if lay["bones"] is not None:
            w = [struct.unpack_from(endian + "f", chunk, off)[0]
                 for off in lay["weights"]]
            total = sum(w)
            w = [max(0.0, v) for v in w]
            w.append(max(0.0, 1.0 - total))
            bi = tuple(chunk[lay["bones"]:lay["bones"] + 4])
            mesh.bone_weights.append(tuple(w))
            mesh.bone_indices.append(bi)


def _half(value):
    """Decode an IEEE 754 half float."""
    sign = -1.0 if value & 0x8000 else 1.0
    exp = (value >> 10) & 0x1F
    frac = value & 0x3FF
    if exp == 0:
        return sign * frac * 2.0 ** -24
    if exp == 31:
        return 0.0 if not frac else float("nan")
    return sign * (1.0 + frac / 1024.0) * 2.0 ** (exp - 15)


def _pack_half(value):
    import math
    if value != value:
        return 0
    sign = 0x8000 if value < 0 else 0
    value = abs(value)
    if value == 0.0:
        return sign
    if value >= 65504.0:
        return sign | 0x7C00
    exp = int(math.floor(math.log2(value)))
    if exp < -14:
        frac = int(round(value * 2.0 ** 24))
        return sign | min(0x3FF, frac)
    frac = int(round((value / 2.0 ** exp - 1.0) * 1024.0))
    if frac == 1024:
        frac = 0
        exp += 1
    if exp > 15:
        return sign | 0x7C00
    return sign | ((exp + 15) << 10) | (frac & 0x3FF)


def unpack111110(value):
    """Decode one 11-11-10 signed packed triple into a float vector."""
    x = value & 0x7FF
    y = (value >> 11) & 0x7FF
    z = (value >> 22) & 0x3FF
    if x >= 0x400:
        x -= 0x800
    if y >= 0x400:
        y -= 0x800
    if z >= 0x200:
        z -= 0x400
    return (x / 1023.0, y / 1023.0, z / 511.0)


def pack111110(vec):
    def q(v, scale, bias):
        return max(0, min(scale, int(round(v * bias))))
    x = q(vec[0], 0x7FF, 1023.0) & 0x7FF
    y = q(vec[1], 0x7FF, 1023.0) & 0x7FF
    z = q(vec[2], 0x3FF, 511.0) & 0x3FF
    return x | (y << 11) | (z << 22)


def _frame_normal(chunk, lay):
    """Return the first vector of the stored orthonormal frame.

    The real surface normal is re-computed from the triangles; the stored
    frame is only used as a fallback for degenerate meshes.
    """
    kind, off = lay["frame"]
    try:
        if kind == "floats":
            return struct.unpack_from("<3f", chunk, off)
        return unpack111110(struct.unpack_from(">I", chunk, off)[0])
    except (struct.error, IndexError):
        return (0.0, 0.0, 1.0)


def _decode_faces(mesh, endian, payload):
    """[u32 count][count * 3 u16 indices][u32 0]"""
    if len(payload) < 8:
        return
    face_count = struct.unpack_from(endian + "I", payload, 0)[0]
    if face_count == 0 or 4 + face_count * 6 > len(payload):
        return
    data = payload[4:4 + face_count * 6]
    faces = []
    limit = len(mesh.positions)
    for i in range(face_count):
        a, b, c = struct.unpack_from(endian + "3H", data, i * 6)
        if max(a, b, c) < limit:
            faces.append((a, b, c))
    mesh.faces = faces


def _unpack_normal(value):
    x = (value & 0x3FF) / 511.5 - 1.0
    y = ((value >> 10) & 0x3FF) / 511.5 - 1.0
    z = ((value >> 20) & 0x3FF) / 511.5 - 1.0
    return (x, y, z)


def pack_normal(x, y, z):
    def q(v):
        return max(0, min(1023, int(round((v + 1.0) * 511.5))))
    return q(x) | (q(y) << 10) | (q(z) << 20)


def _tail_strings(tail, endian=">"):
    """Extract length-prefixed paths/names from a mesh tail."""
    out = []
    i = 0
    n = len(tail)
    while i + 4 < n:
        ln = struct.unpack_from(endian + "I", tail, i)[0]
        if 3 <= ln <= 200 and i + 4 + ln <= n:
            chunk = tail[i + 4:i + 4 + ln]
            if all(32 <= b < 127 for b in chunk) and (b"/" in chunk or b"." in chunk):
                out.append(chunk.decode("ascii"))
                i += 4 + ln
                continue
        i += 1
    return out


# -------------------------------------------------------------- materials
def _parse_material_palette(data, endian, roots):
    """Parse every ZMAT record found at file level.

    The palette sits at the end of the file behind a small section header
    ``[u32 0x4000][u32 count][u32 pathLen][path]...`` and every record is
    preceded by its own length prefixed path.
    """
    materials = []
    marker = _tag_bytes("ZMAT", endian)
    i = 0
    while True:
        i = data.find(marker, i)
        if i < 0:
            break
        mat = _parse_material(data, endian, i)
        if mat is not None:
            materials.append(mat)
        i += 4
    return materials


def _parse_material(data, endian, off):
    """[ZMAT][u32 size][u32 nameLen][name][ZMAT][u32 pathLen][path]
    [u32 0][u32 propCount][u32 propBytes][properties]"""
    mat = GeoMaterial()
    start = off
    marker = _tag_bytes("ZMAT", endian)
    try:
        size = struct.unpack_from(endian + "I", data, off + 4)[0]
        name_len = struct.unpack_from(endian + "I", data, off + 8)[0]
        if not (0 < name_len <= 256) or not (0 < size <= len(data)):
            return None
        p = off + 12
        name = data[p:p + name_len]
        if not _printable(name):
            return None
        mat.name = name.decode("ascii")
        p += name_len
        if data[p:p + 4] != marker:
            return None
        p += 4
        path_len = struct.unpack_from(endian + "I", data, p)[0]
        if path_len > 1024 or p + 4 + path_len > len(data):
            return None
        p += 4
        path = data[p:p + path_len]
        if path and not _printable(path):
            return None
        mat.path = path.decode("latin-1")
        p += path_len
        # [u32 0][u32 property count][u32 unknown]
        pad, count, _extra = struct.unpack_from(endian + "3I", data, p)
        if pad != 0 or not (0 <= count <= 512):
            return None
        p += 12
        # The property stream ends at the record boundary; ``count`` is exact
        # but the third word is not a byte length.
        end = min(start + size + 8, len(data))
        mat.size = size
        mat.properties = _parse_properties(data, endian, p, end)
        mat.raw = data[start:min(start + size + 8, len(data))]
        return mat
    except (struct.error, IndexError):
        return None


def _parse_properties(data, endian, p, end):
    """Property stream: [u32 nameLen][name][u16 type][u16 count][value]."""
    props = {}
    guard = 0
    while p + 8 <= end and guard < 512:
        guard += 1
        nlen = struct.unpack_from(endian + "I", data, p)[0]
        if nlen == 0 or nlen > 128 or p + 8 + nlen > end:
            break
        name = data[p + 4:p + 4 + nlen].decode("latin-1")
        q = p + 4 + nlen
        ptype, count = struct.unpack_from(endian + "HH", data, q)
        q += 4
        try:
            if ptype in (1, 6):
                value = struct.unpack_from(endian + "f", data, q)[0]
                q += 4
            elif ptype == 2:
                value = list(struct.unpack_from(endian + "2f", data, q))
                q += 8
            elif ptype == 3:
                value = list(struct.unpack_from(endian + "3f", data, q))
                q += 12
            elif ptype == 4:
                value = list(struct.unpack_from(endian + "4f", data, q))
                q += 16
            elif ptype == 5:
                value = list(struct.unpack_from(
                    endian + "%df" % max(1, count), data, q))
                q += 4 * count
            elif ptype == 7:
                value = bool(data[q])
                q += 1
            elif ptype == 8:
                value = struct.unpack_from(endian + "i", data, q)[0]
                q += 4
            elif ptype == 9:
                # [u8 present][u8 length][chars][u8 pad]  (a single 0 when empty)
                if data[q] == 0:
                    value = ""
                    q += 1
                else:
                    slen = data[q + 1]
                    if q + 3 + slen > end:
                        break
                    value = data[q + 2:q + 2 + slen].decode("latin-1")
                    q += 3 + slen
            else:
                break
        except (struct.error, IndexError):
            break
        props[name.lower()] = value
        p = q
    return props


def _preceding_string(data, endian, off):
    """Return the length-prefixed name stored directly in front of ``off``.

    Every mesh (and every material) is introduced by ``[u32 len][name]`` in the
    bytes *before* its record, sharing the slot with the file header for the
    first one.
    """
    for length in range(3, 512):
        start = off - length
        if start - 4 < 0:
            break
        if struct.unpack_from(endian + "I", data, start - 4)[0] != length:
            continue
        chunk = data[start:off]
        if _printable(chunk) and (b"/" in chunk or b"." in chunk):
            return chunk.decode("ascii")
    return None


def _assign_materials(geo):
    """Best-effort association between meshes and the file material palette.

    The palette is a flat list; there is no explicit mesh -> material index in
    the records, so the mesh path is matched against the material paths.
    """
    if not geo.materials:
        return
    by_path = {mat.path.lower(): mat for mat in geo.materials if mat.path}
    by_name = {}
    for mat in geo.materials:
        by_name.setdefault(mat.name.lower(), mat)
    by_leaf = {}
    for mat in geo.materials:
        if mat.path:
            by_leaf.setdefault(mat.path.rsplit("/", 1)[-1].lower(), mat)
    for mesh in geo.meshes:
        key = (mesh.model_path or "").lower()
        material = by_path.get(key)
        if material is None and key:
            leaf = key.rsplit("/", 1)[-1]
            material = by_leaf.get(leaf)
        if material is None and key:
            # Longest palette path that is a prefix of the mesh name.
            best = None
            for mat in geo.materials:
                if not mat.path:
                    continue
                prefix = mat.path.lower()
                if key.startswith(prefix) and (best is None or len(prefix) > len(best.path)):
                    best = mat
            material = best
        if material is None and key:
            leaf = key.rsplit("/", 1)[-1]
            best = None
            for name, mat in by_leaf.items():
                if len(name) >= 4 and name in leaf and (best is None or len(name) > len(best[0])):
                    best = (name, mat)
            if best is not None:
                material = best[1]
        if material is None:
            material = by_name.get("main_static") or geo.materials[0]
        mesh.material = material
        mesh.material_path = material.path


# ---------------------------------------------------------------- writing
def _write_mesh(w, mesh):
    if not mesh.needs_rebuild:
        # Nothing was edited: re-emit the original block region verbatim so the
        # file stays byte identical (some blocks deliberately overlap the next
        # block header by four bytes).
        head = list(mesh.head_ints)
        w.tag("GMES" if mesh.tag == "GMES" else mesh.tag)
        w.u32(4 + len(mesh.blocks_raw) + len(mesh.tail))
        w.u32(head[0])
        w.raw(mesh.blocks_raw)
        w.raw(mesh.tail)
        return

    updated = _rebuild_block_payloads(w.endian, mesh, mesh.blocks)
    # payload = [face_count][block0 id][block0 checksum][block0 size]
    #           [block0 tag][block0 tag][block0 payload]
    #           [blockN id][blockN checksum][blockN size][tag][tag][payload]...
    payload_len = 4 + sum(20 + len(block.payload) for block in updated)
    head = list(mesh.head_ints)
    if updated:
        head[1], head[2], head[3] = updated[0].id, updated[0].checksum, len(updated[0].payload)
    head[0] = len(mesh.faces) if mesh.faces else mesh.head_ints[0]

    w.tag("GMES" if mesh.tag == "GMES" else mesh.tag)
    w.u32(payload_len)
    w.u32(head[0])
    w.u32(head[1])
    w.u32(head[2])
    w.u32(head[3])
    for index, block in enumerate(updated):
        if index:
            w.u32(block.id)
            w.u32(block.checksum)
            w.u32(len(block.payload))
        w.tag(block.tag)
        w.tag(block.tag)
        w.raw(block.payload)
    w.raw(mesh.tail)


def _rebuild_block_payloads(endian, mesh, blocks):
    """Replace the vertex/face payloads with the current mesh data."""
    out = []
    for block in blocks:
        if block.tag == "VBFL" and len(block.payload) >= 16:
            fmt, fmt2, count = struct.unpack_from(endian + "3I", block.payload, 0)
            # [fmt][fmt2][count] then count * stride vertex bytes, then a 4-byte footer
            stride = (len(block.payload) - 16) // count if count else 0
            if stride == mesh.stride and mesh.needs_rebuild:
                extra = block.payload[12 + stride * count:]
                payload = _encode_vertices(endian, mesh, fmt, fmt2)
                payload += extra
                out.append(GeoBlock(block.id, block.checksum, block.tag, payload, block.offset))
                continue
        elif block.tag == "ZFBU" and len(block.payload) >= 8 and mesh.needs_rebuild:
            payload = _encode_faces(endian, mesh)
            out.append(GeoBlock(block.id, block.checksum, block.tag, payload, block.offset))
            continue
        out.append(block)
    return out


def _encode_vertices(endian, mesh, fmt, fmt2):
    """Rebuild one VBFL payload from the edited mesh data.

    Each vertex is written from its original raw bytes so that attributes the
    add-on does not understand (the tangent frame, unknown words) survive an
    edit round trip.
    """
    lay = vertex_layout(endian, mesh.stride)
    stride = mesh.stride
    blank = bytes(stride)
    if mesh.vertex_raw:
        blank = bytes(mesh.vertex_raw[0][:stride]).ljust(stride, b"\x00")
    w = Writer(endian)
    w.u32(fmt)
    w.u32(fmt2)
    w.u32(len(mesh.positions))
    for i, p in enumerate(mesh.positions):
        buf = bytearray(mesh.vertex_raw[i][:stride] if i < len(mesh.vertex_raw)
                        else blank)
        if len(buf) < stride:
            buf.extend(b"\x00" * (stride - len(buf)))
        struct.pack_into(endian + "f", buf, lay["x"], p[0])
        struct.pack_into(endian + "f", buf, lay["y"], p[1])
        struct.pack_into(endian + "f", buf, lay["z"], p[2])
        if stride >= 28:
            u, v = mesh.uvs[i] if i < len(mesh.uvs) else (0.0, 0.0)
            uword = _pack_half(u) | (_pack_half(v) << 16)
            struct.pack_into(endian + "I", buf, lay["uv"], uword)
            c = mesh.colors[i] if i < len(mesh.colors) else (1, 1, 1, 1)
            struct.pack_into(endian + "I", buf, lay["color"], _pack_color(c))
        if lay["bones"] is not None and i < len(mesh.bone_weights):
            weights = mesh.bone_weights[i]
            for k, off in enumerate(lay["weights"]):
                value = weights[k] if k < len(weights) else 0.0
                struct.pack_into(endian + "f", buf, off, float(value))
            idx = mesh.bone_indices[i] if i < len(mesh.bone_indices) else (0, 0, 0, 0)
            for k in range(4):
                buf[lay["bones"] + k] = int(max(0, min(255,
                    idx[k] if k < len(idx) else 0)))
        w.raw(bytes(buf))
    return w.bytes()


def _pack_color(c):
    r, g, b, a = c
    return ((int(round(a * 255)) & 255) << 24) | ((int(round(r * 255)) & 255) << 16) \
        | ((int(round(g * 255)) & 255) << 8) | (int(round(b * 255)) & 255)


def _encode_faces(endian, mesh):
    """[u32 count][count * 3 u16 indices][u32 0]"""
    w = Writer(endian)
    w.u32(len(mesh.faces))
    for face in mesh.faces:
        w.u16(face[0])
        w.u16(face[1])
        w.u16(face[2])
    w.u32(0)
    return w.bytes()
