"""Parser/serialiser for Final Exam `.zwo` scene files.

Layout (all integers/floats use the file's byte order):

    u32      0x24
    tag      ZROO
    tag      ZROO
    u32      path_length
    byte[]   path                       (no NUL terminator, no padding)
    byte[105] root_stub                 (identical in every shipped file)
    u32      child_count                number of children of the scene root
    node[]   pre-order depth-first tree
    byte[]   trailer                    a single "ZNAM" record

A node record is:

    tag       (ZENT, ZBON, ZLIT, ZNOD, ZSKI, ZCAM, ...)
    tag       duplicate of the tag (all node types except ZNAM)
    u32       name_length
    byte[]    name
    u32       path_length
    byte[]    path
    f32[3]    position
    f32[4]    rotation quaternion (x, y, z, w)
    f32[3]    scale
    byte[]    type specific tail
    u32       child_count               (always the last 4 bytes of the record)

The type specific tail is kept verbatim so that re-exporting an imported file
is lossless.  Only the transform, name and path are decoded.

Record boundaries cannot be derived arithmetically (tails are not a fixed
size and are not even 4-byte aligned), so the parser scans for tags and then
validates each candidate by checking the duplicate tag and the name length.
"""

import struct

from .binio import Reader, Writer, detect_endian

#: Tags that start a scene-graph node.
NODE_TAGS = (
    "ZENT", "ZBON", "ZLIT", "ZNOD", "ZSKI", "ZCAM",
    "ZBDO", "ZLFC", "ZSKA", "ZSKN", "ZMAC",
    # Collision-style nodes: empty name and path, but a full transform.
    "CABO",
)
#: Stand-alone records that are not part of the tree.
OTHER_TAGS = ("ZNAM", "ZMAT")
ALL_TAGS = NODE_TAGS + OTHER_TAGS

ROOT_STUB_LEN = 105
TRANSFORM_LEN = 40


class ZwoNode:
    """One scene-graph node."""

    __slots__ = ("tag", "name", "path", "position", "rotation", "scale",
                 "tail", "child_count", "children", "opaque", "source_index")

    def __init__(self, tag="ZENT"):
        self.tag = tag
        self.name = ""
        self.path = ""
        self.position = (0.0, 0.0, 0.0)
        self.rotation = (0.0, 0.0, 0.0, 1.0)
        self.scale = (1.0, 1.0, 1.0)
        self.tail = b""
        self.child_count = 0
        self.children = []
        self.opaque = None          # full raw record bytes when it cannot be decoded
        self.source_index = -1

    # -- helpers ------------------------------------------------------
    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, name):
        for node in self.walk():
            if node.name.lower() == name.lower():
                return node
        return None

    def matrix(self):
        """Return the local transform as a 4x4 row-major tuple of tuples."""
        x, y, z, w = self.rotation
        n = (x * x + y * y + z * z + w * w) ** 0.5
        if n < 1e-9:
            x, y, z, w = 0.0, 0.0, 0.0, 1.0
        else:
            x, y, z, w = x / n, y / n, z / n, w / n
        xx, yy, zz = x * x, y * y, z * z
        xy, xz, yz = x * y, x * z, y * z
        wx, wy, wz = w * x, w * y, w * z
        r = ((1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy)),
             (2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx)),
             (2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy)))
        sx, sy, sz = self.scale
        px, py, pz = self.position
        return (
            (r[0][0] * sx, r[0][1] * sy, r[0][2] * sz, px),
            (r[1][0] * sx, r[1][1] * sy, r[1][2] * sz, py),
            (r[2][0] * sx, r[2][1] * sy, r[2][2] * sz, pz),
            (0.0, 0.0, 0.0, 1.0),
        )

    def __repr__(self):
        return "<ZwoNode %s %r children=%d>" % (self.tag, self.name, len(self.children))


class ZwoFile:
    def __init__(self):
        self.endian = "<"
        self.magic = 0x24
        self.path = ""
        self.root_stub = b"\x00" * ROOT_STUB_LEN
        self.children = []          # children of the scene root
        self.trailer = b""          # raw ZNAM record (or empty)
        self.source = None

    # -- parsing ------------------------------------------------------
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
        r = Reader(data, endian)
        obj = cls()
        obj.endian = endian
        obj.magic = r.u32()
        if obj.magic != 0x24:
            raise ValueError("not a ZWO file (magic=%#x)" % obj.magic)
        t1, t2 = r.tag(), r.tag()
        if t1 != "ZROO":
            raise ValueError("not a ZWO file (tag=%r)" % t1)
        obj.path = r.string()
        body = r.pos
        obj.root_stub = data[body:body + ROOT_STUB_LEN]
        if len(obj.root_stub) != ROOT_STUB_LEN:
            raise ValueError("truncated ZWO root")
        root_children = struct.unpack_from(endian + "I", data, body + ROOT_STUB_LEN)[0]

        records, trailer_off = _scan_records(data, endian, body + ROOT_STUB_LEN + 4)
        nodes = [_decode_record(data, endian, rec) for rec in records]

        # Rebuild the tree from the pre-order child counts.
        cursor = [0]

        def build():
            if cursor[0] >= len(nodes):
                return None
            node = nodes[cursor[0]]
            node.source_index = cursor[0]
            cursor[0] += 1
            for _ in range(node.child_count):
                child = build()
                if child is None:
                    break
                node.children.append(child)
            return node

        for _ in range(root_children):
            child = build()
            if child is None:
                break
            obj.children.append(child)

        # Anything after the tree is a stand-alone trailer (normally one ZNAM).
        if trailer_off is not None and trailer_off <= len(data):
            start = records[-1]["end"] if records else body
            obj.trailer = data[records[cursor[0]]["off"]:] if cursor[0] < len(records) else b""
        return obj

    # -- writing ------------------------------------------------------
    def save(self, path):
        with open(path, "wb") as fh:
            fh.write(self.serialize())

    def serialize(self):
        w = Writer(self.endian)
        w.u32(0x24)
        w.tag("ZROO")
        w.tag("ZROO")
        w.string(self.path)
        w.raw(self.root_stub)
        w.u32(len(self.children))
        for child in self.children:
            _write_node(w, child)
        if self.trailer:
            w.raw(self.trailer)
        return w.bytes()

    def walk(self):
        for child in self.children:
            yield from child.walk()


def _scan_records(data, endian, start):
    """Locate every valid record in the file.

    Returns (records, trailer_offset).  Each record dict holds off/end/tag.
    """
    wanted = {}
    for tag in ALL_TAGS:
        raw = tag.encode("ascii")
        if endian == "<":
            raw = raw[::-1]
        wanted[raw] = tag

    n = len(data)
    hits = []
    i = start
    while i <= n - 4:
        tag = wanted.get(data[i:i + 4])
        if tag is not None:
            end = _validate(data, endian, i, tag)
            if end is not None:
                hits.append({"off": i, "tag": tag, "name_end": end})
                i += 4
                continue
        i += 1

    for idx, rec in enumerate(hits):
        rec["end"] = hits[idx + 1]["off"] if idx + 1 < len(hits) else n

    trailer = None
    for rec in hits:
        if rec["tag"] in ("ZNAM", "ZMAT"):
            trailer = rec["off"]
            break
    return hits, trailer


def _validate(data, endian, off, tag):
    """Return the offset just past the path, or None if this is not a record."""
    n = len(data)
    p = off + 4
    if tag not in ("ZNAM",):
        if data[p:p + 4] != data[off:off + 4]:
            return None
        p += 4
    if p + 4 > n:
        return None
    name_len = struct.unpack_from(endian + "I", data, p)[0]
    if name_len > 260 or p + 4 + name_len > n:
        return None
    if name_len == 0:
        # Unnamed nodes exist in map files: the path is empty as well and a
        # full transform follows.  Validate that so we do not accept random
        # bytes that merely look like a tag.
        if p + 8 > n:
            return None
        path_len = struct.unpack_from(endian + "I", data, p + 4)[0]
        if path_len != 0 or p + 8 + TRANSFORM_LEN + 4 > n:
            return None
        pos = struct.unpack_from(endian + "3f", data, p + 8)
        rot = struct.unpack_from(endian + "4f", data, p + 20)
        scale = struct.unpack_from(endian + "3f", data, p + 36)
        norm = sum(v * v for v in rot) ** 0.5
        if not (0.9 < norm < 1.1):
            return None
        if any(abs(v) > 1e6 for v in pos + scale):
            return None
        return p + 8
    for byte in data[p + 4:p + 4 + name_len]:
        if byte < 0x20 or byte > 0x7E:
            return None
    return p + 4 + name_len


def _decode_record(data, endian, rec):
    off, end, tag = rec["off"], rec["end"], rec["tag"]
    node = ZwoNode(tag)
    p = off + 4
    if tag not in ("ZNAM",):
        if data[p:p + 4] == data[off:off + 4]:
            p += 4
    name_len = struct.unpack_from(endian + "I", data, p)[0]
    p += 4
    node.name = data[p:p + name_len].decode("latin-1")
    p += name_len

    if p + 4 <= end:
        path_len = struct.unpack_from(endian + "I", data, p)[0]
        if 0 <= path_len <= 4096 and p + 4 + path_len <= end:
            p += 4
            node.path = data[p:p + path_len].decode("latin-1")
            p += path_len

    body_len = end - p
    if body_len >= TRANSFORM_LEN + 4:
        node.position = struct.unpack_from(endian + "3f", data, p)
        node.rotation = struct.unpack_from(endian + "4f", data, p + 12)
        node.scale = struct.unpack_from(endian + "3f", data, p + 28)
        node.tail = data[p + TRANSFORM_LEN:end - 4]
        node.child_count = struct.unpack_from(endian + "I", data, end - 4)[0]
        if node.child_count > 65535:
            node.child_count = 0
    else:
        node.opaque = data[off:end]
        node.child_count = 0
    return node


def _write_node(w, node):
    if node.opaque is not None:
        w.raw(node.opaque)
        return
    w.tag(node.tag)
    if node.tag not in ("ZNAM",):
        w.tag(node.tag)
    w.string(node.name or "")
    w.string(node.path or "")
    w.vec(node.position)
    w.vec(node.rotation)
    w.vec(node.scale)
    w.raw(node.tail)
    w.u32(len(node.children))
    for child in node.children:
        _write_node(w, child)
