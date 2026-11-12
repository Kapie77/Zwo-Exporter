"""Final Exam `.ani` skeletal animation files.

Layout (verified against the 1,953 shipped PC animations):

    u32     0x4000
    u32     1
    u32     path_length
    byte[]  path
    tag     ZAND
    ...     small header, byte lengths vary; preserved verbatim
    tag     NOAN + u32 count      rest-pose (skeleton) entries
    tag     MAAN + u32 count      morph / blend-shape entries
    tag     APNA + u32 count      parameter animation entries
    tag     ANEV + u32 count      event entries
    u32     track_count
    byte[]  record list: NOAN/MAAN/ANPA records, 18 bytes each plus a
            name descriptor for MAAN/ANPA
    u32     key_track_count       (repeats the track count)
    byte[]  keyframe tracks

Keyframe track header (36 bytes)::

    tag     NCTK / TCTK / RTFK / 1LFK / UCTK / VCTK
    u32     target hash (CRC32 of the lower-case, alphanumeric target name)
    float   six rest values (scale xyz, translation xyz for transform tracks)
    u32     key count

Key payloads::

    NCTK  u32 time_ms, 4 x int16 quaternion            12 bytes per key
    TCTK  u32 time_ms, u32 flags, 4 x int16 quaternion 16 bytes per key
    RTFK  u32 time_ms, 4 x f32 quat, 3 x f32 pos,
          3 x f32 scale                                44 bytes per key
    1LFK  u32 time_ms, f32 value                        8 bytes per key
    UCTK  u32 time_ms, 24 bytes of packed data         28 bytes per key
    VCTK  packed/compressed; not decoded

Quaternions are ``x, y, z, w`` as signed 16-bit values divided by 32767.
``UCTK`` and ``VCTK`` are compressed streams that the game decodes at runtime;
they are kept as raw bytes and simply flagged as unsupported.

The whole file is preserved verbatim so an import/export round-trip never
loses the parts this module does not model.
"""

import math
import struct
import zlib

from .binio import Reader, detect_endian

#: Logical record tags in the header count table.
TRACK_TAGS = ("NOAN", "MAAN", "ANPA", "ANEV")

#: Logical keyframe track tags and their key stride in bytes.
KEY_STRIDE = {
    "NCTK": 12,     # rotation, 4 x int16
    "TCTK": 16,     # rotation + 4 flag bytes, 4 x int16
    "RTFK": 44,     # full transform, 4 + 3 + 3 floats
    "1LFK": 8,      # single float curve
    "UCTK": 28,     # packed transform stream (not decoded)
    "VCTK": 16,     # compressed stream (not decoded)
}

#: Track header size shared by every keyframe track.
KEY_HEADER = 36

ANI_FPS = 30.0


ALL_TAGS = TRACK_TAGS + tuple(KEY_STRIDE)


def _tag_variants(tag):
    """Both byte orders of a tag.

    The shipped PC files store the record tags reversed (``NAON`` for
    ``NOAN``) but keep the keyframe tags literal (``NCTK``), so matching both
    spellings is the only portable option; the PS3 build is not available to
    confirm its convention.
    """
    raw = tag.encode("latin-1")
    return {raw, raw[::-1]}


def _match_tag(raw, endian):
    """Return the logical tag for four raw bytes, or ``None``."""
    for tag in ALL_TAGS:
        if raw in _tag_variants(tag):
            return tag
    return None


def hash_name(name):
    """CRC32 of the lower-case alphanumeric form of a node name.

    This is the hash used by ``NOAN``/``MAAN``/``ANPA`` records.
    """
    key = "".join(ch for ch in name.lower() if ch.isalnum())
    return zlib.crc32(key.encode("latin-1")) & 0xFFFFFFFF


def track_hash(animation_path, name):
    """Hash stored in a keyframe track header for a skeletal target.

    Verified against the shipped assets: it is the CRC32 of the animation
    path (``common/.../anims/walk``) followed by ``/`` and the lower-case
    node name, without any character stripping, e.g.::

        crc32("common/.../anims/caught/joeneck") == 0xE5DE44C1
    """
    key = animation_path.rstrip("/") + "/" + name.lower()
    return zlib.crc32(key.encode("latin-1")) & 0xFFFFFFFF


def property_hash(name):
    """Hash stored in a keyframe track header for a property (``xfov``...)."""
    return zlib.crc32(name.lower().encode("latin-1")) & 0xFFFFFFFF


class AniKey:
    """One keyframe of a track."""

    __slots__ = ("time", "quat", "position", "scale", "extra", "value")

    def __init__(self, time, quat=None, position=None, scale=None,
                 extra=0, value=None):
        self.time = time
        self.quat = quat
        self.position = position
        self.scale = scale
        self.extra = extra
        self.value = value

    def __repr__(self):
        if self.quat is not None:
            body = "q=%s" % (tuple(round(v, 3) for v in self.quat),)
        elif self.value is not None:
            body = "v=%g" % self.value
        else:
            body = ""
        return "<AniKey t=%d %s>" % (self.time, body)


class AniTrack:
    """A keyframe track.  ``kind`` is one of the :data:`KEY_STRIDE` tags."""

    def __init__(self, tag, target_hash, rest, offset=0):
        self.tag = tag
        self.target_hash = target_hash
        self.rest = tuple(rest)
        self.offset = offset
        self.keys = []
        self.raw = b""
        self.name = ""

    @property
    def kind(self):
        return {"NCTK": "rotation", "TCTK": "rotation",
                "RTFK": "transform", "1LFK": "float",
                "UCTK": "packed", "VCTK": "compressed"}.get(self.tag, "unknown")

    @property
    def decoded(self):
        return self.kind in ("rotation", "transform", "float")

    @property
    def duration(self):
        return self.keys[-1].time if self.keys else 0

    def __repr__(self):
        return "<AniTrack %s %#010x %r keys=%d>" % (
            self.tag, self.target_hash, self.name, len(self.keys))

    def name_for(self, lookup):
        """Fill ``name`` from ``lookup`` (a ``{hash: name}`` mapping)."""
        self.name = lookup.get(self.target_hash, self.name)
        return self.name


class AniRecord:
    """A NOAN/MAAN/ANPA/ANEV declaration entry."""

    def __init__(self, tag, target_hash, index=0, name="", entries=None):
        self.tag = tag
        self.target_hash = target_hash
        self.index = index
        self.name = name
        self.entries = entries or []
        self.offset = 0

    def __repr__(self):
        return "<AniRecord %s #%d name=%r>" % (self.tag, self.index, self.name)


class AniFile:
    def __init__(self):
        self.endian = "<"
        self.path = ""
        self.counts = {}
        self.records = []
        self.tracks = []
        self.raw = b""
        self.source = None
        self.track_count = 0
        self.key_track_count = 0
        self.unsupported = []

    # -- accessors ---------------------------------------------------
    @property
    def rest_records(self):
        return [r for r in self.records if r.tag == "NOAN"]

    @property
    def animated_tracks(self):
        return [t for t in self.tracks if t.kind in ("rotation", "transform")]

    @property
    def duration(self):
        return max([t.duration for t in self.tracks] or [0])

    @property
    def frame_range(self):
        """(first, last) frame at :data:`ANI_FPS`."""
        return (0.0, self.duration / 1000.0 * ANI_FPS)

    def __repr__(self):
        return "<AniFile %r records=%d tracks=%d animated=%d>" % (
            self.path, len(self.records), len(self.tracks),
            len(self.animated_tracks))

    # -- names -------------------------------------------------------
    def resolve_names(self, names):
        """Attach names to tracks/records through ``hash_name``.

        ``names`` is any iterable of strings (bone or node names).
        Unknown hashes keep an empty name.
        """
        lookup = {}
        for name in names:
            lookup[hash_name(name)] = name
        for track in self.tracks:
            track.name_for(lookup)
        for record in self.records:
            record.name = lookup.get(record.target_hash, record.name)
        return lookup

    # -- writing -----------------------------------------------------
    def serialize(self):
        """Animations are preserved verbatim; editing them is not supported."""
        return self.raw


def load_ani(path):
    with open(path, "rb") as fh:
        return parse_ani(fh.read(), path)


def _find_any(data, tag, start=0):
    best = -1
    for raw in _tag_variants(tag):
        pos = data.find(raw, start)
        if pos >= 0 and (best < 0 or pos < best):
            best = pos
    return best


def _find_count_table(data, endian):
    """Return the offset of the ``NOAN count MAAN count ...`` table."""
    tags = list(TRACK_TAGS)
    pos = _find_any(data, tags[0])
    while pos >= 0:
        ok = True
        for index, tag in enumerate(tags):
            at = pos + 8 * index
            if at + 4 > len(data) or _match_tag(data[at:at + 4], endian) != tag:
                ok = False
                break
        if ok and pos + 8 * len(tags) + 4 <= len(data):
            return pos
        pos = _find_any(data, tags[0], pos + 1)
    return -1


def _looks_like_track(data, offset, endian, limit):
    """True if a keyframe track header can be parsed at ``offset``."""
    name = _match_tag(data[offset:offset + 4], endian)
    if name not in KEY_STRIDE:
        return None
    if offset + KEY_HEADER > limit:
        return None
    count = struct.unpack_from(endian + "I", data, offset + 32)[0]
    if not 0 < count <= 100000:
        return None
    size = KEY_HEADER + count * KEY_STRIDE[name]
    stop = offset + size
    if stop > limit:
        return None
    if stop < limit:
        if _match_tag(data[stop:stop + 4], endian) is None:
            return None
    rest = struct.unpack_from(endian + "6f", data, offset + 8)
    if not all(math.isfinite(v) and abs(v) < 1e5 for v in rest):
        return None
    return name, count, size


def _key_tracks(data, endian, start):
    """Locate every keyframe track by validated scanning.

    Unknown compressed records (``UCTK``/``VCTK``) can sit between tracks, so a
    straight linear walk is not possible; each candidate is validated against
    its own size and the tag that follows it instead.
    """
    out = []
    pos = start
    cursor = start
    while cursor < len(data) - KEY_HEADER:
        found = _looks_like_track(data, cursor, endian, len(data))
        if found is None:
            cursor += 1
            continue
        tag, count, size = found
        out.append((cursor, tag, count, size))
        cursor += size
    return out


def _decode_keys(data, endian, offset, tag, count):
    stride = KEY_STRIDE[tag]
    keys = []
    for index in range(count):
        key_at = offset + index * stride
        time = struct.unpack_from(endian + "I", data, key_at)[0]
        if tag in ("NCTK", "TCTK"):
            quat_at = key_at + stride - 8
            values = struct.unpack_from(endian + "4h", data, quat_at)
            quat = tuple(v / 32767.0 for v in values)
            extra = 0
            if tag == "TCTK":
                extra = struct.unpack_from(endian + "I", data, key_at + 4)[0]
            keys.append(AniKey(time, quat=quat, extra=extra))
        elif tag == "RTFK":
            quat = struct.unpack_from(endian + "4f", data, key_at + 4)
            position = struct.unpack_from(endian + "3f", data, key_at + 20)
            scale = struct.unpack_from(endian + "3f", data, key_at + 32)
            keys.append(AniKey(time, quat=quat, position=position, scale=scale))
        elif tag == "1LFK":
            value = struct.unpack_from(endian + "f", data, key_at + 4)[0]
            keys.append(AniKey(time, value=value))
        else:
            keys.append(AniKey(time))
    return keys


def parse_ani(data, source=None):
    endian = detect_endian(data)
    ani = AniFile()
    ani.endian = endian
    ani.source = source
    ani.raw = data

    r = Reader(data, endian)
    if r.u32() != 0x4000:
        raise ValueError("not an ANI file")
    r.u32()
    ani.path = r.string()

    if r.peek_tag() != "ZAND":
        return ani
    r.tag()

    table = _find_count_table(data, endian)
    if table < 0:
        return ani

    pos = table
    for tag in TRACK_TAGS:
        count = struct.unpack_from(endian + "I", data, pos + 4)[0]
        ani.counts[tag] = count
        pos += 8
    if pos + 4 > len(data):
        return ani
    ani.track_count = struct.unpack_from(endian + "I", data, pos)[0]
    pos += 4
    record_start = pos

    # Rest-pose entries are fixed size and always come first.
    for _ in range(ani.counts.get("NOAN", 0)):
        if pos + 18 > len(data):
            break
        if _match_tag(data[pos:pos + 4], endian) != "NOAN":
            break
        record = AniRecord("NOAN",
                           struct.unpack_from(endian + "I", data, pos + 4)[0],
                           struct.unpack_from(endian + "I", data, pos + 12)[0])
        record.offset = pos
        ani.records.append(record)
        pos += 18

    # MAAN/ANPA records follow; both carry a name descriptor.
    for _ in range(ani.counts.get("MAAN", 0) + ani.counts.get("ANPA", 0)):
        if pos + 18 > len(data):
            break
        raw_tag = data[pos:pos + 4]
        tag = _match_tag(raw_tag, endian)
        if tag not in ("MAAN", "ANPA", "ANEV"):
            break
        target_hash = struct.unpack_from(endian + "I", data, pos + 4)[0]
        index = struct.unpack_from(endian + "I", data, pos + 12)[0]
        name = ""
        next_pos = pos + 18
        if next_pos + 8 <= len(data):
            length = struct.unpack_from(endian + "I", data, next_pos + 4)[0]
            if 0 <= length <= 256 and next_pos + 12 + length <= len(data):
                name = data[next_pos + 8:next_pos + 8 + length].decode(
                    "latin-1", "replace")
                next_pos += 12 + length
        record = AniRecord(tag, target_hash, index, name)
        record.offset = pos
        ani.records.append(record)
        pos = next_pos

    # Every keyframe track is located independently and then decoded.
    for offset, tag, count, size in _key_tracks(data, endian, record_start):
        rest = struct.unpack_from(endian + "6f", data, offset + 8)
        track = AniTrack(tag,
                         struct.unpack_from(endian + "I", data, offset + 4)[0],
                         rest, offset)
        track.raw = data[offset:offset + size]
        track.keys = _decode_keys(data, endian, offset + KEY_HEADER, tag, count)
        if tag in ("UCTK", "VCTK"):
            ani.unsupported.append(track)
        ani.tracks.append(track)
    ani.tracks.sort(key=lambda item: item.offset)
    return ani
