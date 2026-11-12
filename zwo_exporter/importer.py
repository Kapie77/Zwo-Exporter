"""Blender-side importer for Final Exam `.zwo` scenes.

The format layer (``zwo``, ``geo``, ``hvt``, ``ani``) stays free of ``bpy`` so it
can be tested with plain Python; everything Blender specific lives here.

Only the PC build is targeted.  The PS3 assets share the same record layout but
use the opposite byte order and packed vertex frames, both of which the format
layer already handles.
"""

import math
import os
import tempfile

import bpy
from mathutils import Matrix, Quaternion, Vector

from .zwo import ZwoFile
from .geo import GeoFile
from .hvt import load_hvt, texture_path_for_asset
from .ani import load_ani, track_hash

#: Rotation that takes the engine's Y-up model space into Blender's Z-up.
YUP_TO_ZUP = Matrix(((1.0, 0.0, 0.0, 0.0),
                     (0.0, 0.0, -1.0, 0.0),
                     (0.0, 1.0, 0.0, 0.0),
                     (0.0, 0.0, 0.0, 1.0)))

MESH_NODE_TAGS = ("ZENT", "ZSKI")
_BONE_SUFFIX = "_zwo_bone"


# ----------------------------------------------------------------- helpers
def asset_root_for(path):
    """Return the directory that contains the ``common``/``maps`` folders."""
    parts = os.path.abspath(path).replace("\\", "/").split("/")
    for index in range(len(parts) - 1, 0, -1):
        candidate = "/".join(parts[:index])
        if os.path.isdir(os.path.join(candidate, "common")) or \
                os.path.isdir(os.path.join(candidate, "maps")):
            return candidate
    return os.path.dirname(os.path.abspath(path))


def _texture_dir():
    path = os.path.join(tempfile.gettempdir(), "zwo_exporter_textures")
    os.makedirs(path, exist_ok=True)
    return path


def _node_matrix(node, parent_matrix):
    return parent_matrix @ Matrix(node.matrix())


def _walk_with_matrices(nodes, parent_matrix):
    identity = Matrix.Identity(4)
    for node in nodes:
        matrix = _node_matrix(node, parent_matrix)
        yield node, matrix
        yield from _walk_with_matrices(node.children, matrix)


def _mesh_nodes(zwo):
    return [node for node in zwo.walk() if node.tag in MESH_NODE_TAGS]


def _associate(meshes, nodes):
    """Map meshes to scene nodes.

    Every mesh names its source in ``model_path`` (``.../map_00_a/background18``)
    and the matching node carries that same leaf name (``background18``).  Maps
    instance one mesh under many nodes, so the pair list follows the node list
    and may contain the same mesh several times.
    """
    if not nodes:
        return [(mesh, None) for mesh in meshes]

    by_leaf = {}
    for mesh in meshes:
        leaf = (mesh.model_path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if leaf and leaf not in by_leaf:
            by_leaf[leaf] = mesh

    pairs = []
    used = set()
    unmatched_nodes = []
    for node in nodes:
        mesh = by_leaf.get(node.name.lower())
        if mesh is not None:
            pairs.append((mesh, node))
            used.add(id(mesh))
        else:
            unmatched_nodes.append(node)

    # Anything without a name match (or an asset with no names at all) falls
    # back to positional pairing so nothing is dropped.
    remaining = [m for m in meshes if id(m) not in used]
    for mesh, node in zip(remaining, unmatched_nodes or nodes):
        pairs.append((mesh, node))
    if not pairs:
        pairs = list(zip(meshes, nodes))
    return pairs


# ---------------------------------------------------------------- materials
def _material_key(name):
    return "zwo:" + name


def _build_material(mat, asset_root, use_textures, report):
    key = _material_key(mat.path or mat.name or "material")
    existing = bpy.data.materials.get(key)
    if existing is not None:
        return existing

    material = bpy.data.materials.new(key)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    props = mat.properties

    colour = mat.diffuse_color
    alpha = colour[3]
    opacity = props.get("opacity")
    if isinstance(opacity, (int, float)):
        alpha = float(opacity)
    alpha = max(0.0, min(1.0, alpha))
    bsdf.inputs["Base Color"].default_value = (colour[0], colour[1], colour[2], 1.0)
    bsdf.inputs["Alpha"].default_value = alpha

    gloss = props.get("glossiness")
    if isinstance(gloss, (int, float)):
        gloss = float(gloss)
        # Glossiness is either 0..1 or a Phong specular power.
        roughness = 1.0 - gloss if 0.0 <= gloss <= 1.0 \
            else (2.0 / (gloss + 2.0)) ** 0.5
        bsdf.inputs["Roughness"].default_value = max(0.02, min(1.0, roughness))
    if "Specular IOR Level" in bsdf.inputs:
        value = props.get("specularmultiplier")
        if isinstance(value, (int, float)):
            bsdf.inputs["Specular IOR Level"].default_value = max(0.0, min(1.0, float(value)))
    bsdf.inputs["Metallic"].default_value = 0.0

    if props.get("twosided"):
        material.use_backface_culling = False
    if alpha < 0.999 or props.get("usealpha") or props.get("alpha"):
        if hasattr(material, "surface_render_method"):
            material.surface_render_method = "DITHERED"
        if hasattr(material, "blend_method"):
            material.blend_method = "BLEND"
    if props.get("additive") and hasattr(material, "blend_type"):
        material.blend_type = "ADD"

    if use_textures and mat.texture_path:
        path = texture_path_for_asset(mat.texture_path, asset_root)
        if path:
            image = _load_texture(path, report)
            if image is not None:
                tex = nodes.new("ShaderNodeTexImage")
                tex.image = image
                tex.location = (-520, 320)
                links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
                if alpha < 0.999 or props.get("usealpha"):
                    links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
        else:
            report["missing_textures"].add(mat.texture_path)

    normal_name = props.get("normaltexture")
    if use_textures and normal_name and props.get("usenormaltexture", True):
        path = texture_path_for_asset(normal_name, asset_root)
        if path:
            image = _load_texture(path, report)
            if image is not None:
                image.colorspace_settings.name = "Non-Color"
                tex = nodes.new("ShaderNodeTexImage")
                tex.image = image
                tex.location = (-620, -60)
                nmap = nodes.new("ShaderNodeNormalMap")
                nmap.location = (-260, -60)
                strength = props.get("normalmapboost")
                if isinstance(strength, (int, float)) and 0 < strength <= 2:
                    nmap.inputs["Strength"].default_value = float(strength)
                links.new(tex.outputs["Color"], nmap.inputs["Color"])
                links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
        else:
            report["missing_textures"].add(normal_name)
    return material


def _load_texture(path, report):
    try:
        texture = load_hvt(path)
        if not texture.valid:
            report["bad_textures"].add(path)
            return None
        dds = texture.save_dds(_texture_dir())
    except Exception:
        report["bad_textures"].add(path)
        return None
    for image in bpy.data.images:
        if image.filepath and os.path.normcase(bpy.path.abspath(image.filepath)) == \
                os.path.normcase(dds):
            return image
    try:
        image = bpy.data.images.load(dds, check_existing=True)
    except RuntimeError:
        report["bad_textures"].add(path)
        return None
    image.name = "%s (%s)" % (os.path.splitext(os.path.basename(path))[0],
                              texture.format)
    return image


# ------------------------------------------------------------------- meshes
def _build_mesh_object(mesh, node, matrix, collection, asset_root, options, report):
    name = node.name if node else "mesh_%03d" % max(0, mesh.index)
    data = bpy.data.meshes.new(name + "_mesh")
    # `.geo` vertices and the node transforms share one Z-up space, so they are
    # imported unchanged.
    verts = list(mesh.positions)
    faces = [f for f in mesh.faces]
    data.from_pydata(verts, [], faces)
    data.validate(verbose=False)

    if mesh.uvs and len(data.loops):
        layer = data.uv_layers.new(name="UVMap")
        uvs = mesh.uvs
        for loop in data.loops:
            index = loop.vertex_index
            if index < len(uvs):
                layer.data[loop.index].uv = (uvs[index][0], 1.0 - uvs[index][1])

    for polygon in data.polygons:
        polygon.use_smooth = True
    try:
        data.set_sharp_from_angle(angle=0.6981317)     # 40 degrees
    except (AttributeError, TypeError):
        pass

    data.update()

    obj = bpy.data.objects.new(name, data)
    # Vertices were converted to Z-up above, and scene nodes are already Z-up,
    # so the node matrix is applied unchanged.
    obj.matrix_world = matrix
    collection.objects.link(obj)
    obj["zwo_mesh_stride"] = mesh.stride
    obj["zwo_mesh_tag"] = mesh.tag
    obj["zwo_mesh_index"] = mesh.index

    if node is not None:
        obj["zwo_node"] = node.source_index
        obj["zwo_node_name"] = node.name
        obj["zwo_node_tag"] = node.tag
    if mesh.bone_names:
        obj["zwo_bones"] = ";".join(mesh.bone_names)

    material = mesh.material
    if material is None and report["geo"] is not None and report["geo"].materials:
        material = report["geo"].materials[0]
    if material is not None:
        data.materials.append(_build_material(material, asset_root,
                                             options.get("textures", True), report))
    return obj


def _build_armature(zwo, nodes_with_matrices, collection, report):
    bones = [node for node, _m in nodes_with_matrices if node.tag == "ZBON"]
    if not bones:
        return None, {}
    matrices = {node.source_index: matrix for node, matrix in nodes_with_matrices}

    arm_data = bpy.data.armatures.new("zwo_armature")
    arm_obj = bpy.data.objects.new("zwo_armature", arm_data)
    collection.objects.link(arm_obj)
    arm_obj.matrix_world = Matrix.Identity(4)

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    created = {}
    for node in bones:
        matrix = matrices[node.source_index]
        head = matrix.to_translation()
        # The bone rest matrix must equal the scene node's world matrix: skinning
        # and animation both deform relative to the bone rest, so a head/tail
        # approximation (with an arbitrary roll) would twist the mesh once posed.
        bone = arm_data.edit_bones.new(node.name)
        bone.head = head
        bone.tail = head + Vector((0.0, 0.1, 0.0))
        bone.matrix = matrix
        children = [c for c in node.children if c.tag == "ZBON"]
        if children:
            tail = matrices[children[0].source_index].to_translation()
            length = (tail - head).length
        else:
            length = 0.12
        bone.length = max(0.02, min(length, 5.0))
        created[node.source_index] = bone

    for node in bones:
        bone = created[node.source_index]
        for child in node.children:
            if child.tag == "ZBON" and child.source_index in created:
                bone_ = created[child.source_index]
                bone_.parent = bone

    bpy.ops.object.mode_set(mode="OBJECT")
    report["armature"] = arm_obj
    return arm_obj, created


def _bind_vertex_groups(obj, mesh, arm_obj):
    if not mesh.bone_names or not mesh.bone_weights:
        return 0
    groups = {}
    for name in mesh.bone_names:
        short = name.rsplit("/", 1)[-1]
        bone = arm_obj.data.bones.get(short)
        if bone is None:
            for candidate in arm_obj.data.bones:
                if candidate.name.lower() == short.lower():
                    bone = candidate
                    break
        if bone is None:
            continue
        groups[name] = obj.vertex_groups.new(name=bone.name)
    bound = 0
    for index, weights in enumerate(mesh.bone_weights):
        indices = mesh.bone_indices[index]
        names = mesh.bone_names
        total = 0.0
        pairs = []
        for slot, weight in enumerate(weights):
            if slot >= len(indices):
                break
            if weight <= 1e-5:
                continue
            bone_index = indices[slot]
            if bone_index >= len(names):
                continue
            group = groups.get(names[bone_index])
            if group is None:
                continue
            pairs.append((group, weight))
            total += weight
        if not pairs:
            continue
        for group, weight in pairs:
            group.add([index], weight / total, "REPLACE")
        bound += 1
    modifier = obj.modifiers.new("Armature", "ARMATURE")
    modifier.object = arm_obj
    obj.parent = arm_obj
    obj.matrix_parent_inverse = arm_obj.matrix_world.inverted()
    return bound


# ---------------------------------------------------------------- animation
class _TrackSampler:
    """Interpolates one keyframe track and produces local matrices."""

    __slots__ = ("track", "keys", "times")

    def __init__(self, track):
        self.track = track
        self.keys = sorted(track.keys, key=lambda key: key.time)
        self.times = [key.time for key in self.keys]

    def _pair(self, time):
        keys = self.keys
        if time <= keys[0].time:
            return keys[0], keys[0], 0.0
        if time >= keys[-1].time:
            return keys[-1], keys[-1], 0.0
        low, high = 0, len(keys) - 1
        while high - low > 1:
            middle = (low + high) // 2
            if keys[middle].time <= time:
                low = middle
            else:
                high = middle
        first, second = keys[low], keys[high]
        span = second.time - first.time
        return first, second, ((time - first.time) / span) if span > 0 else 0.0

    def matrix(self, time, bind_position, bind_scale):
        """Local transform of the target at ``time`` (absolute, not a delta)."""
        if not self.keys:
            return None
        first, second, factor = self._pair(time)
        if self.track.kind == "rotation":
            quat = _file_quat(first.quat).slerp(_file_quat(second.quat), factor)
            # Rotation-only tracks keep a constant translation in the header.
            position = tuple(self.track.rest[3:6]) if len(self.track.rest) >= 6 \
                else bind_position
            if position == (0.0, 0.0, 0.0) and bind_position != (0.0, 0.0, 0.0):
                # Older tracks leave the header empty; keep the bind offset.
                position = bind_position
            scale = bind_scale
            return _compose(position, quat, scale)
        if self.track.kind == "transform":
            quat = _file_quat(first.quat).slerp(_file_quat(second.quat), factor)
            position = tuple(first.position[i] + (second.position[i] - first.position[i]) * factor
                             for i in range(3))
            scale = tuple(first.scale[i] + (second.scale[i] - first.scale[i]) * factor
                          for i in range(3))
            return _compose(position, quat, scale)
        return None


def _file_quat(values):
    """Build a mathutils quaternion from the file's ``x, y, z, w`` order."""
    x, y, z, w = values
    quat = Quaternion((w, x, y, z))
    length = quat.magnitude
    if length < 1e-8:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    return Quaternion((quat.w / length, quat.x / length,
                       quat.y / length, quat.z / length))


def _compose(position, quat, scale):
    return (Matrix.Translation(Vector(position)) @
            quat.to_matrix().to_4x4() @
            Matrix.Diagonal(Vector(scale).to_4d()))


def _find_armature(context):
    active = context.view_layer.objects.active
    if active is not None and active.type == "ARMATURE":
        return active
    for obj in context.selected_objects:
        if obj.type == "ARMATURE":
            return obj
    for obj in context.scene.objects:
        if obj.type == "ARMATURE":
            return obj
    return None


def animation_targets(ani, armature, zwo):
    """Map tracks to armature bones through the animation path hash."""
    node_by_name = {}
    parent_of = {}
    if zwo is not None:
        stack = [(node, None) for node in zwo.children]
        while stack:
            node, parent = stack.pop()
            node_by_name.setdefault(node.name.lower(), node)
            parent_of[id(node)] = parent
            for child in node.children:
                stack.append((child, node))

    bone_node = {}
    bone_parent_node = {}
    if armature is not None:
        for bone in armature.data.bones:
            node = node_by_name.get(bone.name.lower())
            bone_node[bone.name] = node
            if bone.parent is not None:
                bone_parent_node[bone.name] = bone_node.get(bone.parent.name)

    samplers = {}
    for track in ani.tracks:
        if track.kind in ("rotation", "transform"):
            samplers[track.target_hash] = _TrackSampler(track)

    chains = {}
    if armature is not None:
        for bone in armature.data.bones:
            node = bone_node.get(bone.name)
            if node is None:
                continue
            stop = bone_parent_node.get(bone.name)
            chain = []
            cursor = node
            guard = 0
            while cursor is not None and cursor is not stop and guard < 64:
                chain.append(cursor)
                cursor = parent_of.get(id(cursor))
                guard += 1
            chain.reverse()
            chains[bone.name] = chain

    return samplers, chains, bone_node, node_by_name, parent_of


def _node_chain(node, parent_of):
    chain = []
    guard = 0
    while node is not None and guard < 64:
        chain.append(node)
        node = parent_of.get(id(node))
        guard += 1
    chain.reverse()
    return chain


def _is_skinned(obj):
    if obj.type != "MESH":
        return False
    if any(modifier.type == "ARMATURE" for modifier in obj.modifiers):
        return True
    return bool(obj.vertex_groups)


def _zwo_for_animation(context, ani_path, armature):
    """Pick the imported asset the animation belongs to.

    Object-only clips (cameras, foliage, props) have no armature, so the
    ``.zwo`` is recovered from the imported objects.  When several assets are
    loaded the one whose path is a prefix of the animation path wins.
    """
    candidates = []
    if armature is not None and armature.get("zwo_file"):
        candidates.append(armature["zwo_file"])
    for obj in context.scene.objects:
        path = obj.get("zwo_file")
        if path:
            candidates.append(path)

    seen = []
    for path in candidates:
        if path not in seen:
            seen.append(path)
    if not seen:
        return None
    if len(seen) == 1:
        return seen[0]

    key = os.path.splitext(ani_path)[0].replace("\\", "/").lower()
    best = None
    for path in seen:
        base = os.path.splitext(path)[0].replace("\\", "/").lower()
        if key.startswith(base) or base in key:
            if best is None or len(base) > len(os.path.splitext(best)[0]):
                best = path
    return best or seen[0]


def import_animation(context, ani_path, armature=None, options=None):
    """Import a `.ani` as an action on the armature imported from a `.zwo`.

    The animation keys are absolute local transforms (verified against the
    shipped assets: rotation-only tracks carry the bind rotation, ``RTFK``
    keys carry the bind position and unit scale).  They are converted to
    pose-bone ``matrix_basis`` values so they render correctly no matter how
    the edit bones were oriented.
    """
    options = options or {}
    fps = float(options.get("fps", 30.0))
    ani = load_ani(ani_path)
    if not ani.tracks:
        raise ValueError("%s contains no keyframe tracks"
                         % os.path.basename(ani_path))

    armature = armature or _find_armature(context)
    if armature is not None and armature.type != "ARMATURE":
        armature = None

    zwo_path = _zwo_for_animation(context, ani_path, armature)
    if armature is not None and zwo_path:
        kept = armature.get("zwo_file")
        if kept and os.path.normcase(os.path.abspath(kept)) != \
                os.path.normcase(os.path.abspath(zwo_path)):
            armature = None

    zwo = None
    if zwo_path and os.path.isfile(zwo_path):
        try:
            zwo = ZwoFile.load(zwo_path)
        except Exception:                             # noqa: BLE001
            zwo = None

    samplers, chains, bone_node, node_by_name, parent_of = animation_targets(
        ani, armature, zwo)

    # Non-skinned objects (props, foliage, cameras, lights) follow the same
    # tracks: their world matrix is rebuilt from the animated node chain.
    object_targets = []
    if node_by_name:
        for obj in context.scene.objects:
            node_name = obj.get("zwo_node_name")
            if not node_name:
                continue
            node = node_by_name.get(str(node_name).lower())
            if node is None or _is_skinned(obj):
                continue
            chain = _node_chain(node, parent_of)
            if not any(samplers.get(track_hash(ani.path, n.name)) is not None
                       for n in chain):
                continue
            object_targets.append((obj, chain))

    times = set()
    for chain in chains.values():
        for node in chain:
            sampler = samplers.get(track_hash(ani.path, node.name))
            if sampler is not None:
                times.update(sampler.times)
    for _obj, chain in object_targets:
        for node in chain:
            sampler = samplers.get(track_hash(ani.path, node.name))
            if sampler is not None:
                times.update(sampler.times)
    if not times:
        raise ValueError("no track of %s targets the imported asset"
                         % os.path.basename(ani_path))
    start = min(times) if options.get("start_at_zero", True) else 0.0
    samples = sorted(times)
    frames = [(time - start) / 1000.0 * fps for time in samples]

    report = {"bones": 0, "objects": 0, "tracks": 0, "start": start,
              "end": max(times), "actions": []}

    # -- pose the armature at every key time and record the basis values
    motion = {}
    for bone in (armature.data.bones if armature is not None else ()):
        chain = chains.get(bone.name)
        if not chain:
            continue
        usable = False
        for node in chain:
            if samplers.get(track_hash(ani.path, node.name)) is not None:
                usable = True
                break
        if not usable:
            continue

        rest = bone.matrix_local
        if bone.parent is not None:
            rest = bone.parent.matrix_local.inverted() @ bone.matrix_local
        inverted = rest.inverted()

        locations, rotations, scales = [], [], []
        for time in samples:
            matrix = Matrix.Identity(4)
            for node in chain:
                sampler = samplers.get(track_hash(ani.path, node.name))
                local = None
                if sampler is not None:
                    local = sampler.matrix(time, node.position, node.scale)
                if local is None:
                    local = Matrix(node.matrix())
                matrix = matrix @ local
            basis = inverted @ matrix
            location, rotation, scale = basis.decompose()
            rotations.append((rotation.w, rotation.x, rotation.y, rotation.z))
            locations.append((location.x, location.y, location.z))
            scales.append((scale.x, scale.y, scale.z))
        motion[bone.name] = (locations, rotations, scales)
        report["bones"] += 1

    actions = []
    armature_action = None
    stem = os.path.splitext(os.path.basename(ani_path))[0]

    if motion:
        armature_action = bpy.data.actions.new(stem)
        armature_action.use_fake_user = True
        if armature.animation_data is None:
            armature.animation_data_create()
        armature.animation_data.action = armature_action
        actions.append(armature_action)

        for bone_name, (locations, rotations, scales) in motion.items():
            pose_bone = armature.pose.bones.get(bone_name)
            if pose_bone is not None:
                pose_bone.rotation_mode = "QUATERNION"

            base = 'pose.bones["%s"].%%s' % bone_name.replace('"', '\\"')
            _write_curves(armature_action, base, locations, rotations, scales,
                          frames, bone_name)

    # -- object-level curves, one action per object (4.2 actions are not shared
    # between different ID types).
    for obj, chain in object_targets:
        locations, rotations, scales = [], [], []
        for time in samples:
            world = Matrix.Identity(4)
            for node in chain:
                sampler = samplers.get(track_hash(ani.path, node.name))
                local = None
                if sampler is not None:
                    local = sampler.matrix(time, node.position, node.scale)
                world = world @ (local if local is not None else Matrix(node.matrix()))
            location, rotation, scale = world.decompose()
            rotations.append((rotation.w, rotation.x, rotation.y, rotation.z))
            locations.append((location.x, location.y, location.z))
            scales.append((scale.x, scale.y, scale.z))

        action = bpy.data.actions.new("%s_%s" % (stem, obj.name))
        action.use_fake_user = True
        if obj.animation_data is None:
            obj.animation_data_create()
        obj.animation_data.action = action
        obj.rotation_mode = "QUATERNION"
        _write_curves(action, "%s", locations, rotations, scales, frames,
                      obj.name)
        action.use_frame_range = True
        action.frame_start = frames[0]
        action.frame_end = frames[-1]
        actions.append(action)
        report["objects"] += 1

    if not actions:
        raise ValueError("no node of %s matches this animation"
                         % os.path.basename(ani_path))

    if armature_action is not None:
        armature_action.use_frame_range = True
        armature_action.frame_start = frames[0]
        armature_action.frame_end = frames[-1]
    if options.get("scene_range", True):
        context.scene.frame_start = int(math.floor(frames[0]))
        context.scene.frame_end = int(math.ceil(frames[-1]))

    report["action"] = armature_action
    report["actions"] = actions
    report["duration"] = max(times) - start
    return report


def _write_curves(action, base, locations, rotations, scales, frames, group):
    """Write location/rotation/scale curves at the shared frame list."""
    # Keep quaternion signs continuous so component-wise interpolation never
    # takes the long way around.
    for index in range(1, len(rotations)):
        previous, current = rotations[index - 1], rotations[index]
        if sum(a * b for a, b in zip(previous, current)) < 0.0:
            rotations[index] = tuple(-value for value in current)

    for prop, values in (("location", locations),
                         ("rotation_quaternion", rotations),
                         ("scale", scales)):
        width = len(values[0])
        for index in range(width):
            curve = action.fcurves.new(base % prop, index=index,
                                       action_group=group)
            curve.keyframe_points.add(len(frames))
            for slot, (frame, vector) in enumerate(zip(frames, values)):
                point = curve.keyframe_points[slot]
                point.co = (frame, vector[index])
                point.interpolation = "LINEAR"
            curve.update()


# -------------------------------------------------------------------- main
def import_asset(context, zwo_path, options=None):
    """Import one `.zwo` (plus its `.geo`/`.hvt`) into the current scene."""
    options = options or {}
    report = {
        "meshes": 0, "vertices": 0, "faces": 0, "skinned": 0, "bounds": 0,
        "missing_textures": set(), "bad_textures": set(), "geo": None,
        "armature": None, "warnings": [],
    }

    base, _ext = os.path.splitext(zwo_path)
    geo_path = base + ".geo"
    asset_root = asset_root_for(zwo_path)

    zwo = ZwoFile.load(zwo_path)
    geo = GeoFile.load(geo_path) if os.path.isfile(geo_path) else None
    report["geo"] = geo
    if geo is None:
        report["warnings"].append("no sibling .geo file")

    collection = bpy.data.collections.new(os.path.basename(base))
    context.scene.collection.children.link(collection)

    nodes = list(_walk_with_matrices(zwo.children, Matrix.Identity(4)))
    arm_obj, _bones = _build_armature(zwo, nodes, collection, report)
    if arm_obj is not None:
        arm_obj["zwo_file"] = zwo_path
    matrices = {node.source_index: matrix for node, matrix in nodes}

    if geo is not None and geo.meshes:
        mesh_nodes = [node for node, _m in nodes if node.tag in MESH_NODE_TAGS]
        pairs = _associate(geo.meshes, mesh_nodes)
        if len(pairs) != len(geo.meshes):
            report["warnings"].append(
                "%d meshes instanced across %d nodes"
                % (len(geo.meshes), len(pairs)))
        for mesh, node in pairs:
            matrix = matrices.get(node.source_index) if node else Matrix.Identity(4)
            obj = _build_mesh_object(mesh, node, matrix, collection,
                                     asset_root, options, report)
            obj["zwo_file"] = zwo_path
            obj["geo_file"] = geo_path
            report["meshes"] += 1
            report["vertices"] += len(mesh.positions)
            report["faces"] += len(mesh.faces)
            if arm_obj is not None and mesh.bone_weights:
                _bind_vertex_groups(obj, mesh, arm_obj)
                report["skinned"] += 1
        report["bounds"] = len(mesh_nodes)

    if options.get("lights", True):
        _build_lights(zwo, nodes, collection, report, zwo_path)
    if options.get("cameras", False):
        _build_cameras(zwo, nodes, collection, report, zwo_path)

    return report


def _build_lights(zwo, nodes, collection, report, zwo_path=None):
    for node, matrix in nodes:
        if node.tag != "ZLIT":
            continue
        light_data = bpy.data.lights.new(node.name or "light", type="POINT")
        light_data.energy = 100.0
        light_obj = bpy.data.objects.new(node.name or "light", light_data)
        light_obj.matrix_world = matrix
        collection.objects.link(light_obj)
        light_obj["zwo_node"] = node.source_index
        light_obj["zwo_node_name"] = node.name
        if zwo_path:
            light_obj["zwo_file"] = zwo_path
        report.setdefault("lights", 0)
        report["lights"] += 1


def _build_cameras(zwo, nodes, collection, report, zwo_path=None):
    for node, matrix in nodes:
        if node.tag != "ZCAM":
            continue
        camera_data = bpy.data.cameras.new(node.name or "camera")
        camera_obj = bpy.data.objects.new(node.name or "camera", camera_data)
        camera_obj.matrix_world = matrix
        collection.objects.link(camera_obj)
        camera_obj["zwo_node"] = node.source_index
        camera_obj["zwo_node_name"] = node.name
        camera_obj["zwo_tail"] = node.tail.hex()
        if zwo_path:
            camera_obj["zwo_file"] = zwo_path
        report.setdefault("cameras", 0)
        report["cameras"] += 1
