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

from .zwo import ZwoFile, node_bind_matrix, rigid_bind_matrix
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
#: Name of the colour attribute that receives the `.geo` per-vertex tint.
VERTEX_COLOUR_LAYER = "zwo_color"

#: Bones in a `.zwo` run along the scene node's local **+X** (a bone's children
#: sit on that axis), while Blender bones run along their local **+Y**.  The
#: rest matrix therefore needs this fixed roll so the bone follows the limb
#: instead of sticking out sideways.  The animation importer applies the same
#: rotation to the keys, which keeps the deformation identical (a rest frame
#: change alone would twist the mesh once posed).
BONE_AXIS = Matrix.Rotation(-math.pi / 2.0, 4, "Z")
BONE_AXIS_INV = BONE_AXIS.inverted()

#: Length used when a bone cannot reach a child along its own axis (leaf bones
#: and branch children).  Blender bones are drawn head-to-tail, so a bone that
#: follows an off-axis child would visibly spear out of the mesh.
DEFAULT_BONE_LENGTH = 0.12

#: A bone that had to fall back to ``DEFAULT_BONE_LENGTH`` has no length data of
#: its own, and that constant is far longer than short features such as the
#: spitter's claws or the flyer's wing fingers.  ``_fit_leaf_bones`` clips those
#: tails to the mesh so the rig does not look broken.
LEAF_BONE_SAMPLES = 24
LEAF_BONE_MIN_LENGTH = 0.02
LEAF_BONE_MARGIN = 0.005

#: Some rigs are authored with the `.geo` mesh yawed 90 degrees around Z
#: relative to the `.zwo` skeleton (the flyer family).  The detector below is
#: deliberately strict: only a clean, well populated fit is corrected, so
#: symmetric props and characters whose skeleton is merely in a different pose
#: are left alone.
FRAME_FIT_MIN_BONES = 6
FRAME_FIT_ANGLE = (85.0, 95.0)
FRAME_FIT_BASELINE = 0.3
FRAME_FIT_RESIDUAL = 0.3


def _flatten_matrix(matrix):
    return [value for row in matrix for value in row]


def _unflatten_matrix(values):
    return Matrix(tuple(tuple(values[row * 4:(row + 1) * 4])
                        for row in range(4)))


def _mesh_frame_world(obj, world, frame):
    """Carry a rigid node's animated world matrix into the mesh frame.

    The importer places rigid parts (the flyer's wings) at
    ``frame @ world @ bind`` so they sit on the mesh, where ``bind`` is the
    mesh-to-node matrix from the node's tail.  The armature object uses the same
    ``frame``.  The animation stores node-space matrices, so the same
    multiplication restores the placement the engine uses at every key time.
    Skinned parts are handled by the armature modifier and never reach here.
    """
    bind = obj.get("zwo_rigid_bind") if obj is not None else None
    if bind:
        world = world @ _unflatten_matrix(bind)
    if frame is None:
        return world
    return frame @ world


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


def _find_sibling(path, extension):
    """Find ``<stem><extension>`` next to ``path`` (case-insensitive).

    Windows is normally case-insensitive, but asset rips and network shares can
    carry upper-case extensions such as ``BASIC.GEO``.  Returning ``None``
    lets the caller distinguish a missing file from a parse error.
    """
    base = os.path.splitext(path)[0]
    direct = base + extension
    if os.path.isfile(direct):
        return direct
    folder = os.path.dirname(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        name_stem, name_ext = os.path.splitext(name)
        if name_stem.lower() == stem and name_ext.lower() == extension.lower():
            return os.path.join(folder, name)
    return None


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


def _mask_colour(nodes, links, value_socket, location):
    """Expand a scalar mask into a grey colour so it can feed a Mix node."""
    combine = nodes.new("ShaderNodeCombineColor")
    combine.mode = "RGB"
    combine.location = location
    for channel in ("Red", "Green", "Blue"):
        links.new(value_socket, combine.inputs[channel])
    return combine.outputs[0]


def _build_material(mat, asset_root, use_textures, report, use_vertex_colour=False):
    key = _material_key(mat.path or mat.name or "material")
    if use_vertex_colour:
        key += "#vcol"
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

    # The engine's Phong specular is
    #     SpecularColor * SpecularMultiplier * SpecularTexture
    # gated by `UseSpecular`, and the texture is only sampled when
    # `UseSpecularTexture` is set.  Every shader that shades geometry this way
    # (`collectable`, `main_static`, `main_dyn`, `decal`, `tree`, `monster`)
    # declares those parameters; `character.fx` declares none, so characters
    # have no specular term at all.  Blender's "Specular IOR Level" is the
    # closest equivalent, and a specular mask is exactly what the weapons need:
    # `9mm_spec` is white on the slide and black on the grip, so without it the
    # wooden and rubber parts are as glossy as the metal.
    specular_level = 0.5
    if props.get("usespecular") is False:
        specular_level = 0.0
    else:
        value = props.get("specularmultiplier")
        if isinstance(value, (int, float)):
            specular_level = float(value)
    specular_level = max(0.0, min(1.0, specular_level))
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = specular_level
        tint = props.get("specularcolor")
        if isinstance(tint, (list, tuple)) and len(tint) >= 3 and all(
                isinstance(c, (int, float)) for c in tint[:3]) and any(
                    abs(float(c) - 1.0) > 0.02 for c in tint[:3]):
            bsdf.inputs["Specular Tint"].default_value = (
                float(tint[0]), float(tint[1]), float(tint[2]), 1.0)
    bsdf.inputs["Metallic"].default_value = 0.0

    # `twosided` is only written when a material is *not* two-sided, so its
    # presence with a false value means the game culls the back faces.  Blender
    # renders both sides by default, which exposes the inside of one-sided
    # shells: the schoolkids' hair shows the blood decal its inner faces point
    # at, and the extra layer z-fights with the outer hair.  Absent means the
    # material is two-sided, so culling stays off.
    twosided = props.get("twosided")
    if twosided is not None:
        material.use_backface_culling = not twosided

    # Characters use "usealpha" as a cut-out mask (hair, nets, straps), not as
    # smooth transparency: blending the whole material makes the model look
    # washed out, so the alpha is thresholded into a dithered cut-out instead.
    # Real translucency (opacity < 1 or additive) keeps smooth blending.
    smooth_alpha = isinstance(opacity, (int, float)) and float(opacity) < 0.99
    additive = bool(props.get("additive"))
    clip_alpha = bool(props.get("usealpha") or props.get("alpha")) and not smooth_alpha

    if additive and hasattr(material, "blend_type"):
        material.blend_type = "ADD"
    if smooth_alpha or additive:
        if hasattr(material, "surface_render_method"):
            material.surface_render_method = "BLENDED"
        if hasattr(material, "blend_method"):
            material.blend_method = "BLEND"
    elif clip_alpha:
        if hasattr(material, "surface_render_method"):
            material.surface_render_method = "DITHERED"
        if hasattr(material, "blend_method"):
            material.blend_method = "CLIP"

    base_socket = None
    alpha_socket = None
    vcol_socket = None
    base_image = None

    # `.geo` vertex colours tint the atlas: the white/neutral parts of the
    # texture take their colour per vertex (backpack, skin, monster hide, ...).
    if use_vertex_colour:
        vcol = nodes.new("ShaderNodeVertexColor")
        vcol.layer_name = VERTEX_COLOUR_LAYER
        vcol.location = (-760, 120)
        vcol_socket = vcol.outputs["Color"]

    # "_onlycolordiffuse" is a neutral 4x4 placeholder: the real colour comes
    # from `diffusecolor` and the vertex colours (tentacles, nests, phials, ...).
    diffuse_name = mat.texture_path
    placeholder = bool(diffuse_name) and \
        os.path.basename(diffuse_name).lower() == "_onlycolordiffuse"

    # `diffusecolor` is the material tint the shader multiplies into the base
    # colour -- ``diffuse * texture * vertex colour``.  Level art uses it to
    # recolour one shared atlas: the road, the concrete and the decals each come
    # in several tints, the basket's `_onlycolordiffuse` body is green, and the
    # map's boundary plane (`black`) is pure black.  The key is absent when a
    # material is not tinted; the parsed default of 0.8 grey is *not* a tint.
    tint_value = props.get("diffusecolor")
    tint = None
    if isinstance(tint_value, (list, tuple)) and len(tint_value) >= 3 and all(
            isinstance(c, (int, float)) for c in tint_value[:3]):
        tint = (float(tint_value[0]), float(tint_value[1]), float(tint_value[2]), 1.0)
        if all(abs(c - 1.0) <= 0.02 for c in tint[:3]):
            tint = None

    # The shipped monster shader (shadow0/monster.fx) does not tint the texture
    # with the vertex colour: it uses the red channel as a blend mask between
    # the lit diffuse texture and the blob-shading ramp.  White keeps the
    # texture (the "Hitter" body, which is genuinely dark brown in the shipped
    # atlas); red = 0 selects the unlit ramp (green head, mouth and other
    # organic patches).  Character materials still multiply the whole vertex
    # colour into the atlas.
    #
    # The engine picks a shader from the material's parameter set.  Every
    # `monster.zfx` variant takes an env map, while `monster_wing.zfx` and the
    # standalone `blob.zfx` rim pass do not, so the presence of
    # `envmaptexture` is what separates "blob shaded" from "just a ramp".
    #
    # `monster_wing.zfx` only reads the red channel of the vertex colour (it
    # animates the membrane), so those materials must not be tinted by RGB.
    blob_name = props.get("blobshadingtexture")
    blob_shader = bool(blob_name) and bool(props.get("envmaptexture"))
    vcol_tint = vcol_socket is not None and not blob_name

    if use_textures and diffuse_name and not placeholder:
        path = texture_path_for_asset(diffuse_name, asset_root)
        if path:
            image = _load_texture(path, report)
            if image is not None:
                base_image = image
                tex = nodes.new("ShaderNodeTexImage")
                tex.image = image
                tex.location = (-520, 320)
                if vcol_tint:
                    mix = nodes.new("ShaderNodeMix")
                    mix.data_type = "RGBA"
                    mix.blend_type = "MULTIPLY"
                    mix.location = (-240, 320)
                    mix.inputs["Factor"].default_value = 1.0
                    links.new(tex.outputs["Color"], mix.inputs[6])
                    links.new(vcol_socket, mix.inputs[7])
                    base_socket = mix.outputs[2]
                else:
                    base_socket = tex.outputs["Color"]
                if smooth_alpha or additive:
                    alpha_socket = tex.outputs["Alpha"]
                elif clip_alpha:
                    clip = nodes.new("ShaderNodeMath")
                    clip.operation = "GREATER_THAN"
                    clip.inputs[1].default_value = 0.5
                    clip.location = (-240, 60)
                    links.new(tex.outputs["Alpha"], clip.inputs[0])
                    alpha_socket = clip.outputs[0]
            else:
                report["bad_textures"].add(path)
        else:
            report["missing_textures"].add(diffuse_name)

    # "blobshadingtexture" is a 1D colour ramp sampled by
    # fresnel = 1 - dot(normal, view).  The game scales it by
    # 4 - 2.5 * LightDarkDefaultFactor (1.5 at the default factor of 1) and
    # then does:
    #
    #     color = lerp(blob, diffuse * (lighting + 0.5), vcol.red)
    #
    # The blob term is never multiplied by the lights, so it is reproduced
    # through the emission socket instead of the base colour:
    #
    #     base     = diffuse * vcol.red
    #     emission = blob * (1 - vcol.red)
    #
    # which is the same sum, with the lit part left for Blender's lights.
    if use_textures and blob_shader and vcol_socket is not None:
        blob_path = texture_path_for_asset(blob_name, asset_root)
        if blob_path:
            blob_image = _load_texture(blob_path, report)
            if blob_image is not None:
                separate = nodes.new("ShaderNodeSeparateColor")
                separate.mode = "RGB"
                separate.location = (-900, 120)
                links.new(vcol_socket, separate.inputs["Color"])
                red = separate.outputs["Red"]

                facing = nodes.new("ShaderNodeLayerWeight")
                facing.inputs["Blend"].default_value = 0.5
                facing.location = (-1100, -320)
                coord = nodes.new("ShaderNodeCombineXYZ")
                coord.location = (-940, -320)
                coord.inputs["Y"].default_value = 0.0
                links.new(facing.outputs["Facing"], coord.inputs["X"])
                ramp = nodes.new("ShaderNodeTexImage")
                ramp.image = blob_image
                ramp.extension = "EXTEND"
                ramp.interpolation = "Linear"
                ramp.location = (-760, -320)
                links.new(coord.outputs[0], ramp.inputs["Vector"])

                scale = nodes.new("ShaderNodeMix")
                scale.data_type = "RGBA"
                scale.blend_type = "MULTIPLY"
                scale.location = (-560, -320)
                scale.inputs["Factor"].default_value = 1.0
                scale.inputs[7].default_value = (1.5, 1.5, 1.5, 1.0)
                links.new(ramp.outputs["Color"], scale.inputs[6])
                blob_socket = scale.outputs[2]

                # base = diffuse * red
                lit = nodes.new("ShaderNodeMix")
                lit.data_type = "RGBA"
                lit.blend_type = "MULTIPLY"
                lit.location = (-320, 320)
                lit.inputs["Factor"].default_value = 1.0
                if base_socket is not None:
                    links.new(base_socket, lit.inputs[6])
                else:
                    lit.inputs[6].default_value = (1.0, 1.0, 1.0, 1.0)
                links.new(_mask_colour(nodes, links, red, (-520, 200)), lit.inputs[7])
                base_socket = lit.outputs[2]

                # emission = blob * (1 - red)
                inverse = nodes.new("ShaderNodeMath")
                inverse.operation = "SUBTRACT"
                inverse.inputs[0].default_value = 1.0
                inverse.location = (-520, -60)
                links.new(red, inverse.inputs[1])

                glow = nodes.new("ShaderNodeMix")
                glow.data_type = "RGBA"
                glow.blend_type = "MULTIPLY"
                glow.location = (-320, -320)
                glow.inputs["Factor"].default_value = 1.0
                links.new(blob_socket, glow.inputs[6])
                links.new(_mask_colour(nodes, links, inverse.outputs[0], (-520, -160)),
                          glow.inputs[7])
                links.new(glow.outputs[2], bsdf.inputs["Emission Color"])
                bsdf.inputs["Emission Strength"].default_value = 1.0
            else:
                report["bad_textures"].add(blob_path)
        else:
            report["missing_textures"].add(blob_name)

    if base_socket is None and vcol_socket is not None:
        base_socket = vcol_socket

    # The tint is applied last so it also reaches the materials that carry no
    # diffuse texture at all (`black`, `_onlycolordiffuse` bodies, decals).
    if tint is not None:
        tinted = nodes.new("ShaderNodeMix")
        tinted.data_type = "RGBA"
        tinted.blend_type = "MULTIPLY"
        tinted.location = (-120, 320)
        tinted.inputs["Factor"].default_value = 1.0
        if base_socket is not None:
            links.new(base_socket, tinted.inputs[6])
        else:
            tinted.inputs[6].default_value = (1.0, 1.0, 1.0, 1.0)
        tinted.inputs[7].default_value = tint
        base_socket = tinted.outputs[2]

    if base_socket is not None:
        links.new(base_socket, bsdf.inputs["Base Color"])
    if alpha_socket is not None:
        links.new(alpha_socket, bsdf.inputs["Alpha"])

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

    # `speculartexture` is a per-pixel specular mask (white on metal, black on
    # wood, rubber and cloth), scaled by `specularmultiplier`.  It is only read
    # when `usespeculartexture` is set: `crowbar` and `grenade` keep the field
    # pointing at their diffuse while the flag is false, and `chainsaw`'s teeth
    # material does the same, so the flag is what decides.
    spec_name = props.get("speculartexture")
    if use_textures and spec_name and props.get("usespeculartexture") and \
            specular_level > 0.0 and "Specular IOR Level" in bsdf.inputs:
        path = texture_path_for_asset(spec_name, asset_root)
        if path:
            image = _load_texture(path, report)
            if image is not None:
                if image is base_image:
                    # `magnum` reuses `magnum_diff` as its mask; a copy keeps
                    # the Non-Color setting away from the base colour.
                    image = image.copy()
                image.colorspace_settings.name = "Non-Color"
                tex = nodes.new("ShaderNodeTexImage")
                tex.image = image
                tex.location = (-620, -320)
                gain = nodes.new("ShaderNodeMath")
                gain.operation = "MULTIPLY"
                gain.use_clamp = True
                gain.location = (-320, -320)
                gain.inputs[1].default_value = specular_level
                links.new(tex.outputs["Color"], gain.inputs[0])
                links.new(gain.outputs[0], bsdf.inputs["Specular IOR Level"])
        else:
            report["missing_textures"].add(spec_name)
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
def _apply_vertex_colours(mesh, data):
    """Copy the `.geo` per-vertex colour into a colour attribute.

    Most meshes store pure white here, which means "use the texture as is".
    Character materials multiply the RGB tint into the atlas (skin, backpacks).
    Monster materials only read the red channel, using it as a mask between the
    lit texture and the blob-shading ramp (see `_build_material`).
    """
    colours = getattr(mesh, "colors", None)
    if not colours or len(colours) != len(data.vertices):
        return False
    tinted = False
    for colour in colours:
        if abs(colour[0] - 1.0) > 1e-3 or abs(colour[1] - 1.0) > 1e-3 \
                or abs(colour[2] - 1.0) > 1e-3:
            tinted = True
            break
    if not tinted:
        return False
    try:
        attribute = data.color_attributes.new(name=VERTEX_COLOUR_LAYER,
                                              type="FLOAT_COLOR", domain="POINT")
    except (AttributeError, TypeError, RuntimeError):
        return False
    for index, colour in enumerate(colours):
        attribute.data[index].color = (colour[0], colour[1], colour[2], 1.0)
    return True


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

    use_vertex_colour = _apply_vertex_colours(mesh, data)

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

    use_textures = options.get("textures", True)
    slots = {}
    calls = getattr(mesh, "draw_calls", [])
    for call in calls:
        material = call.material
        if material is None or id(material) in slots:
            continue
        slots[id(material)] = len(data.materials)
        data.materials.append(_build_material(material, asset_root,
                                             use_textures, report,
                                             use_vertex_colour))
    if not data.materials:
        material = mesh.material
        if material is None and report["geo"] is not None and report["geo"].materials:
            material = report["geo"].materials[0]
        if material is not None:
            data.materials.append(_build_material(material, asset_root,
                                                 use_textures, report,
                                                 use_vertex_colour))
            slots[id(material)] = 0
    if calls:
        if len(data.polygons) == len(mesh.faces):
            for call in calls:
                slot = slots.get(id(call.material))
                if slot is None:
                    continue
                start = call.first_face
                end = min(start + call.face_count, len(data.polygons))
                for index in range(start, end):
                    data.polygons[index].material_index = slot
        else:
            report["warnings"].append(
                "%s: face count changed during import (%d -> %d); "
                "multi-material ranges may be approximate"
                % (name, len(mesh.faces), len(data.polygons)))
    return obj


def _bone_centroids(geo):
    """Weighted vertex centroid of every bone referenced by the `.geo` meshes."""
    centroids = {}
    for mesh in geo.meshes:
        names = [name.rsplit("/", 1)[-1].lower()
                 for name in (mesh.bone_names or [])]
        if not names:
            continue
        for index, position in enumerate(mesh.positions):
            if index >= len(mesh.bone_weights) or index >= len(mesh.bone_indices):
                continue
            weights = mesh.bone_weights[index]
            indices = mesh.bone_indices[index]
            for slot in range(min(len(weights), len(indices))):
                weight = weights[slot]
                if weight <= 1e-4:
                    continue
                bone_index = indices[slot]
                if bone_index >= len(names):
                    continue
                entry = centroids.setdefault(names[bone_index],
                                             [Vector((0.0, 0.0, 0.0)), 0.0])
                entry[0] += Vector(position) * weight
                entry[1] += weight
    return {name: total / weight
            for name, (total, weight) in centroids.items() if weight > 1e-4}


#: Maximum spread allowed between the per-bone implied mesh transforms.  Rigs
#: whose bones disagree (a non-rigid bind, or bind data left over from another
#: mesh) are left to the yaw fit instead of being placed from bad data.
BIND_FRAME_TOLERANCE = 5e-3

#: How far the per-bone implied mesh transform has to disagree before the node
#: tree is treated as "not the bind pose" and the bones are rebuilt from the
#: bind matrices instead.  Rigs that are a centimetre or two out are left alone;
#: the node tree is still the better description of their rest pose.
BIND_POSE_MIN_SPREAD = 2e-2


def _implied_spread(matrices):
    """Largest deviation of a set of 4x4 matrices from their average."""
    average = [[0.0] * 4 for _ in range(4)]
    for matrix in matrices:
        for row in range(4):
            for column in range(4):
                average[row][column] += matrix[row][column] / len(matrices)
    spread = 0.0
    for matrix in matrices:
        for row in range(3):
            for column in range(4):
                spread = max(spread, abs(matrix[row][column] - average[row][column]))
    return spread


def _bind_frame(zwo, nodes):
    """Return the affine mesh transform implied by the bones' bind matrices.

    Every skinned ``ZBON`` stores ``bind = (M @ world) ** -1``.  Solving for
    ``M`` gives the transform that maps the skeleton's node space onto the mesh
    and it is the same for every bone of a well formed rig.  The armature
    *object* gets ``M``, so the bones sit exactly where the engine puts them -
    including the translation that a pure yaw fit cannot recover - while the
    mesh vertices stay untouched and export byte for byte.

    Returns ``None`` when there is no bind data, when the bones disagree, when
    the result is not a rigid transform, or when it is the identity.
    """
    implied = []
    for node, world in nodes:
        if node.tag != "ZBON":
            continue
        bind = node_bind_matrix(node, zwo.endian)
        if bind is None:
            continue
        try:
            implied.append(Matrix(bind).inverted() @ world.inverted())
        except ValueError:
            continue
    if len(implied) < 2:
        return None

    if _implied_spread(implied) > BIND_FRAME_TOLERANCE:
        return None

    average = [[0.0] * 4 for _ in range(4)]
    for matrix in implied:
        for row in range(4):
            for column in range(4):
                average[row][column] += matrix[row][column] / len(implied)
    average = Matrix(average)
    basis = average.to_3x3()
    identity = Matrix.Identity(3)
    orthonormal = basis @ basis.transposed()
    error = max(abs(orthonormal[row][column] - identity[row][column])
                for row in range(3) for column in range(3))
    if error > 1e-2 or basis.determinant() <= 0.0:
        return None

    rotation_error = max(abs(basis[row][column] - identity[row][column])
                         for row in range(3) for column in range(3))
    if rotation_error < 1e-4 and average.to_translation().length < 1e-4:
        return None
    return average


def _bind_pose(zwo, nodes):
    """Return per-bone rest matrices taken straight from the bind data.

    Most rigs store their rest pose in the node tree, so ``bind = (M @ world)** -1``
    only differs from ``world** -1`` by the constant mesh frame ``M`` and moving the
    armature object is enough.  A few (``fatwilly``, ``scientist``, ``nathan``)
    keep a *posed* node tree instead: the nodes carry their own scale and
    rotation, the per-bone implied ``M`` disagrees, and no single object
    transform can line the skeleton up with the mesh - the bones end up rotated
    and floating beside the body.

    The bind matrices themselves are still a clean rigid set, and ``bind** -1`` is
    exactly the bone rest matrix in mesh space (skinning is
    ``v' = world @ bind @ v``, so at rest ``world = bind** -1``).  Building the
    armature from those puts every bone back inside the character.

    Returns ``None`` when the bind data is missing or not rigid, or when the
    node tree already *is* the bind pose (up to ``BIND_POSE_MIN_SPREAD``).
    """
    rest = {}
    implied = []
    for node, world in nodes:
        if node.tag != "ZBON":
            continue
        bind = node_bind_matrix(node, zwo.endian)
        if bind is None:
            return None
        matrix = Matrix(bind)
        basis = matrix.to_3x3()
        if basis.determinant() <= 0.0:
            return None
        orthonormal = basis @ basis.transposed()
        error = max(abs(orthonormal[row][column] - (1.0 if row == column else 0.0))
                    for row in range(3) for column in range(3))
        if error > 1e-2:
            return None
        inverse = matrix.inverted()
        rest[node.source_index] = inverse
        implied.append(inverse @ world.inverted())
    if len(rest) < 2:
        return None
    if _implied_spread(implied) <= BIND_POSE_MIN_SPREAD:
        return None
    return rest


def _frame_rotation(zwo, geo, nodes):
    """Return the Z yaw that puts the skeleton onto the mesh, or ``None``.

    The fit is a 2D Procrustes rotation of the bone heads onto the weighted
    vertex centroids.  Rotating the armature *object* (never the vertices) keeps
    the geometry byte identical for export while making the bones follow the
    limbs.

    This is only the fallback for rigs whose bind matrices are missing or
    inconsistent (see :func:`_bind_frame`).
    """
    if geo is None or not geo.meshes:
        return None
    centroids = _bone_centroids(geo)
    if not centroids:
        return None
    heads, targets = [], []
    for node, matrix in nodes:
        if node.tag != "ZBON":
            continue
        centroid = centroids.get(node.name.lower())
        if centroid is None:
            continue
        heads.append(matrix.to_translation())
        targets.append(centroid)
    if len(heads) < FRAME_FIT_MIN_BONES:
        return None

    mean_head = sum(heads, Vector((0.0, 0.0, 0.0))) / len(heads)
    mean_target = sum(targets, Vector((0.0, 0.0, 0.0))) / len(targets)
    sine = 0.0
    cosine = 0.0
    for head, target in zip(heads, targets):
        head_x, head_y = head.x - mean_head.x, head.y - mean_head.y
        target_x, target_y = target.x - mean_target.x, target.y - mean_target.y
        sine += head_x * target_y - head_y * target_x
        cosine += head_x * target_x + head_y * target_y
    angle = math.atan2(sine, cosine)
    degrees = abs(math.degrees(angle))
    if not FRAME_FIT_ANGLE[0] <= degrees <= FRAME_FIT_ANGLE[1]:
        return None

    rotation = Matrix.Rotation(angle, 4, "Z")
    baseline = 0.0
    residual = 0.0
    for head, target in zip(heads, targets):
        baseline += (head - target).length_squared
        residual += ((rotation @ head) - target).length_squared
    baseline = math.sqrt(baseline / len(heads))
    residual = math.sqrt(residual / len(heads))
    if baseline < FRAME_FIT_BASELINE or residual > FRAME_FIT_RESIDUAL:
        return None
    return rotation


def _build_armature(zwo, nodes_with_matrices, collection, report, rest=None):
    bones = [node for node, _m in nodes_with_matrices if node.tag == "ZBON"]
    if not bones:
        return None, {}
    matrices = {node.source_index: matrix for node, matrix in nodes_with_matrices}
    if rest is not None:
        # Rigs whose node tree is not the bind pose get their rest matrices from
        # the bind data instead (see _bind_pose).
        matrices.update(rest)

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
        # The bone rest matrix must equal the scene node's world matrix (rotated
        # so Blender's +Y bone axis follows the node's +X limb axis): skinning
        # and animation both deform relative to the bone rest, so a head/tail
        # approximation (with an arbitrary roll) would twist the mesh once posed.
        bone = arm_data.edit_bones.new(node.name)
        bone.head = head
        bone.tail = head + Vector((0.0, 0.1, 0.0))
        bone.matrix = matrix @ BONE_AXIS
        children = [c for c in node.children if c.tag == "ZBON"]
        length = DEFAULT_BONE_LENGTH
        if children:
            offset = matrices[children[0].source_index].to_translation() - head
            if offset.length > 1e-6:
                # Follow the child only when it sits on the bone's own axis
                # (within 45 degrees).  Branch children such as a mouth or a
                # second arm hanging off the spine are far to the side, and
                # their distance would spear the bone out through the skin.
                axis = (matrix.to_3x3() @ Vector((1.0, 0.0, 0.0))).normalized()
                along = offset.dot(axis)
                lateral = (offset - axis * along).length
                if along > 0.0 and lateral <= along:
                    length = offset.length
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


def _tag_helper_bones(arm_obj, collection, report):
    """Separate bones that deform the mesh from helper bones that do not.

    Some rigs inherit bones the mesh never uses - ``spitter`` shares the
    four-armed ``jumper`` skeleton, so its second pair of arms exists only as
    bone data.  Those bones cannot move anything, so they are marked
    non-deforming and parked in a hidden bone collection instead of floating
    around the viewport.  Root bones stay visible because they are the handle
    used to move the whole character.
    """
    weighted = set()
    for obj in collection.objects:
        if obj.type != "MESH" or not obj.vertex_groups:
            continue
        names = [group.name for group in obj.vertex_groups]
        for vert in obj.data.vertices:
            for group in vert.groups:
                if group.weight > 1e-4:
                    weighted.add(names[group.group])
    bones = list(arm_obj.data.bones)
    if not bones or not weighted:
        return
    helpers = [bone for bone in bones
               if bone.name not in weighted and bone.parent is not None]
    helper_names = {bone.name for bone in helpers}
    for bone in bones:
        if bone.name not in weighted:
            bone.use_deform = False
    report["helper_bones"] = len(helpers)
    if not helpers:
        return
    deform = arm_obj.data.collections.new("Deform")
    hidden = arm_obj.data.collections.new("Helpers (no weights)")
    for bone in bones:
        if bone.name in helper_names:
            hidden.assign(bone)
        else:
            deform.assign(bone)
    hidden.is_visible = False


def _rigid_bone(mesh):
    """Return the single bone that moves every vertex of ``mesh``, or ``None``."""
    if not mesh.bone_weights:
        return None
    used = set()
    for indices, weights in zip(mesh.bone_indices, mesh.bone_weights):
        for index, weight in zip(indices, weights):
            if weight > 1e-4:
                used.add(index)
    if len(used) != 1:
        return None
    index = used.pop()
    if not (0 <= index < len(mesh.bone_names)):
        return None
    return mesh.bone_names[index].replace("\\", "/").rsplit("/", 1)[-1].lower()


def _world_bounds(obj):
    lo = [1e18, 1e18, 1e18]
    hi = [-1e18, -1e18, -1e18]
    matrix = obj.matrix_world
    for vert in obj.data.vertices:
        point = matrix @ vert.co
        for axis in range(3):
            lo[axis] = min(lo[axis], point[axis])
            hi[axis] = max(hi[axis], point[axis])
    return lo, hi


def _bounds_overlap(first, second):
    """Fraction of the smaller box that the two boxes share."""
    lo_a, hi_a = first
    lo_b, hi_b = second
    volume = 1.0
    volume_a = 1.0
    volume_b = 1.0
    for axis in range(3):
        depth = min(hi_a[axis], hi_b[axis]) - max(lo_a[axis], lo_b[axis])
        volume *= max(0.0, depth)
        volume_a *= max(1e-9, hi_a[axis] - lo_a[axis])
        volume_b *= max(1e-9, hi_b[axis] - lo_b[axis])
    return volume / min(volume_a, volume_b)


#: How much of the smaller mesh has to sit inside the other one before two
#: rigid parts are treated as alternative versions of the same part.
ALTERNATE_OVERLAP = 0.9


def _tag_alternate_meshes(built, collection, report):
    """Park alternate variants of a part in a hidden collection.

    ``holga`` ships two faces - ``holga_head`` and ``holga_headangry`` - that
    are rigidly bound to the same head bone and occupy the same space; the game
    swaps them to change her expression.  Importing both makes them z-fight, so
    the face reads as a broken texture.  The shortest name stays visible (the
    base variant) and the others go to ``Alternates (hidden)``, which can be
    enabled in the outliner to look at the other expression.

    Only parts that are rigidly bound to the *same* single bone, share a name
    prefix and overlap almost completely qualify, so unrelated meshes that
    happen to share a name are left alone.
    """
    groups = {}
    for obj, mesh, node in built:
        if node is None or not obj.vertex_groups:
            continue
        bone = _rigid_bone(mesh)
        if bone is None:
            continue
        groups.setdefault(bone, []).append(obj)
    hidden = []
    for objects in groups.values():
        if len(objects) < 2:
            continue
        bounds = {}
        for index, obj in enumerate(objects):
            for other in objects[index + 1:]:
                if not (obj.name.startswith(other.name)
                        or other.name.startswith(obj.name)):
                    continue
                if obj not in bounds:
                    bounds[obj] = _world_bounds(obj)
                if other not in bounds:
                    bounds[other] = _world_bounds(other)
                if _bounds_overlap(bounds[obj], bounds[other]) < ALTERNATE_OVERLAP:
                    continue
                longer = obj if len(obj.name) > len(other.name) else other
                if longer not in hidden:
                    hidden.append(longer)
    if not hidden:
        return
    parked = bpy.data.collections.new("Alternates (hidden)")
    collection.children.link(parked)
    parked.hide_viewport = True
    parked.hide_render = True
    for obj in hidden:
        for owner in list(obj.users_collection):
            owner.objects.unlink(obj)
        parked.objects.link(obj)
    report["alternate_meshes"] = [obj.name for obj in hidden]


def _surface_offset(trees, point):
    """Signed distance from ``point`` to the nearest of ``trees``.

    Positive values are outside the mesh, negative inside.  When several meshes
    overlap (a body plus rigid wing pieces) the nearest surface wins.
    """
    best = None
    for tree in trees:
        location, normal, _index, distance = tree.find_nearest(point)
        if location is None:
            continue
        signed = -distance if (point - location).dot(normal) < 0.0 else distance
        if best is None or abs(signed) < abs(best):
            best = signed
    return best


def _fit_leaf_bones(arm_obj, collection, report):
    """Shorten bones whose length came from ``DEFAULT_BONE_LENGTH``.

    Leaf bones and bones whose only children sit off their own axis have no
    length data in the file, so ``_build_armature`` gives them a fixed 0.12.
    On short features - the spitter's claws, a toe - that is longer than the
    limb itself, so the drawn tail pokes through the skin and the rig reads as
    misplaced even though the head and direction are right.

    A bone's length only affects how Blender draws it (deformation uses the
    rest matrix), so the tail is clipped to the last sample that is no further
    outside the mesh than the head already is.  Bones whose length came from a
    real child position are left untouched.
    """
    from mathutils.bvhtree import BVHTree

    trees = []
    for obj in collection.objects:
        if obj.type != "MESH" or not obj.data.polygons or not obj.data.vertices:
            continue
        verts = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
        polys = [tuple(poly.vertices) for poly in obj.data.polygons]
        trees.append(BVHTree.FromPolygons(verts, polys))
    if not trees:
        report["shortened_bones"] = 0
        return

    world = arm_obj.matrix_world
    targets = []
    for bone in arm_obj.data.bones:
        if not bone.use_deform or bone.parent is None:
            continue
        head = world @ bone.head_local
        tail = world @ bone.tail_local
        offset = tail - head
        length = offset.length
        if abs(length - DEFAULT_BONE_LENGTH) > 1e-4:
            continue
        axis = offset / length
        limit = max(_surface_offset(trees, head), 0.0) + LEAF_BONE_MARGIN
        if _surface_offset(trees, tail) <= limit:
            # The tail already sits on (or inside) the skin; only tails that
            # spear out are wrong.  Checking the tail first also keeps bones
            # that legitimately cross a cavity (mouth, eye socket) untouched.
            continue
        reach = 0.0
        for step in range(1, LEAF_BONE_SAMPLES + 1):
            distance = length * step / LEAF_BONE_SAMPLES
            if _surface_offset(trees, head + axis * distance) > limit:
                break
            reach = distance
        if reach >= length - 1e-4:
            continue
        new_length = max(LEAF_BONE_MIN_LENGTH, reach)
        if new_length < length - 1e-4:
            targets.append((bone.name, new_length))
    if not targets:
        report["shortened_bones"] = 0
        return

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    for name, length in targets:
        arm_obj.data.edit_bones[name].length = length
    bpy.ops.object.mode_set(mode="OBJECT")
    report["shortened_bones"] = len(targets)


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

    # Rigid parts of a yawed rig have to be carried into the mesh frame too.
    frame = None
    if armature is not None:
        stored = armature.get("zwo_frame_rotation")
        if stored:
            frame = _unflatten_matrix(stored)

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

        # The chain product at bind time is exactly the un-rotated rest of the
        # bone relative to its parent bone (the parent bone's node is excluded
        # from the chain).  Using it instead of ``bone.matrix_local`` avoids a
        # special case for the root, whose rest is ``world @ BONE_AXIS`` rather
        # than ``BONE_AXIS^-1 @ world @ BONE_AXIS``.
        #
        # When the armature was built from the bind matrices instead of the node
        # tree (``zwo_bind_pose``), the node product is *not* the rest the bones
        # were created with, so the equivalent is taken from the rest itself.
        if armature.get("zwo_bind_pose"):
            rest_world = bone.matrix_local @ BONE_AXIS_INV
            if bone.parent is not None:
                parent_world = bone.parent.matrix_local @ BONE_AXIS_INV
                bind = parent_world.inverted() @ rest_world
            else:
                bind = rest_world
        else:
            bind = Matrix.Identity(4)
            for node in chain:
                bind = bind @ Matrix(node.matrix())
        inverted = bind.inverted()

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
            # Conjugating by BONE_AXIS keeps the deformation ``pose @ rest^-1``
            # identical to the engine's while the bone's +Y follows the limb.
            basis = BONE_AXIS_INV @ (inverted @ matrix) @ BONE_AXIS
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
        curves, groups = _action_curves(armature_action, armature)
        actions.append(armature_action)

        for bone_name, (locations, rotations, scales) in motion.items():
            pose_bone = armature.pose.bones.get(bone_name)
            if pose_bone is not None:
                pose_bone.rotation_mode = "QUATERNION"

            base = 'pose.bones["%s"].%%s' % bone_name.replace('"', '\\"')
            _write_curves(curves, groups, base, locations, rotations, scales,
                          frames, bone_name)

    # -- object-level curves, one action per object (actions are not shared
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
            world = _mesh_frame_world(obj, world, frame)
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
        curves, groups = _action_curves(action, obj)
        _write_curves(curves, groups, "%s", locations, rotations, scales,
                      frames, obj.name)
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


def _action_curves(action, owner):
    """Return ``(fcurves, groups)`` for ``action`` bound to ``owner``.

    Blender 4.4 introduced slotted actions and 5.0 removed the legacy
    ``Action.fcurves`` shortcut.  There the curves live in a channelbag that
    belongs to a slot, and the owner has to be pointed at that slot explicitly;
    an action assigned while still empty never gets an automatic slot, which is
    why the slot is created here rather than left to Blender.
    """
    if not hasattr(action, "slots"):
        return action.fcurves, None
    slot = action.slots.new(id_type=owner.id_type, name=owner.name)
    layer = action.layers.new("Layer")
    strip = layer.strips.new(type="KEYFRAME")
    bag = strip.channelbag(slot, ensure=True)
    if owner.animation_data is None:
        owner.animation_data_create()
    owner.animation_data.action = action
    owner.animation_data.action_slot = slot
    return bag.fcurves, bag.groups


def _write_curves(curves, groups, base, locations, rotations, scales, frames,
                  group):
    """Write location/rotation/scale curves at the shared frame list."""
    params = set(curves.bl_rna.functions["new"].parameters.keys())
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
            path = base % prop
            if "group_name" in params:
                curve = curves.new(path, index=index, group_name=group)
            elif "action_group" in params:
                curve = curves.new(path, index=index, action_group=group)
            else:
                # Blender 4.4/4.5 channelbags take no group argument; the
                # group is created on the bag and assigned afterwards.
                curve = curves.new(path, index=index)
                if groups is not None:
                    curve.group = groups.get(group) or groups.new(name=group)
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
        "armature": None, "warnings": [], "helper_bones": 0,
    }

    base, _ext = os.path.splitext(zwo_path)
    geo_path = _find_sibling(zwo_path, ".geo")
    asset_root = asset_root_for(zwo_path)

    zwo = ZwoFile.load(zwo_path)
    geo = GeoFile.load(geo_path) if geo_path else None
    report["geo"] = geo
    report["geo_path"] = geo_path
    mesh_nodes = [node for node in zwo.walk() if node.tag in MESH_NODE_TAGS]
    if geo is None and mesh_nodes:
        hint = ""
        folder = os.path.dirname(os.path.abspath(zwo_path))
        try:
            if not any(name.lower().endswith(".geo") for name in os.listdir(folder)):
                hint = (" This folder has no .geo files at all, so it looks like only "
                        "the .zwo scenes were extracted; re-extract the full game data "
                        "(.geo/.hvt/.ani) or import from the complete data folder.")
        except OSError:
            pass
        # Fail before creating anything so no orphan armature is left behind.
        raise ValueError(
            "no sibling .geo file found (expected %s.geo next to the .zwo).%s"
            % (base, hint))
    if geo is None:
        report["warnings"].append(
            "no sibling .geo file found (expected %s.geo next to the .zwo)" % base)
    elif not geo.meshes:
        report["warnings"].append(
            "%s contains no meshes" % os.path.basename(geo_path))

    collection = bpy.data.collections.new(os.path.basename(base))
    context.scene.collection.children.link(collection)

    nodes = list(_walk_with_matrices(zwo.children, Matrix.Identity(4)))
    # The bones' bind matrices give the exact transform between the skeleton's
    # node space and the mesh (a rotation for some rigs, a rotation plus a
    # translation for the flyer family and joe).  Moving the armature object
    # instead of the vertices keeps the `.geo` data untouched and puts every
    # joint where the engine expects it.
    frame = _bind_frame(zwo, nodes)
    if frame is None:
        # Fallback for rigs without usable bind data: a pure yaw fit.
        frame = _frame_rotation(zwo, geo, nodes)
    rest = None
    if frame is None:
        # Last resort: a rig whose node tree is a pose rather than the bind
        # pose has no single object transform, so the bones are rebuilt from
        # the bind matrices (fatwilly, scientist, nathan).
        rest = _bind_pose(zwo, nodes)
    arm_obj, _bones = _build_armature(zwo, nodes, collection, report, rest)
    if arm_obj is not None:
        arm_obj["zwo_file"] = zwo_path
        if rest is not None:
            arm_obj["zwo_bind_pose"] = True
    if arm_obj is not None and frame is not None:
        arm_obj.matrix_world = frame
        arm_obj["zwo_frame_rotation"] = _flatten_matrix(frame)
    matrices = {node.source_index: matrix for node, matrix in nodes}
    mesh_objects = []

    if geo is not None and geo.meshes:
        mesh_nodes = [node for node, _m in nodes if node.tag in MESH_NODE_TAGS]
        pairs = _associate(geo.meshes, mesh_nodes)
        if len(pairs) != len(geo.meshes):
            report["warnings"].append(
                "%d meshes instanced across %d nodes"
                % (len(geo.meshes), len(pairs)))
        built = []
        unresolved = 0
        for mesh, node in pairs:
            if mesh.is_instance and mesh.source is None:
                # The record points at a mesh that is not in this .geo; there
                # is nothing to place, but the node keeps its bytes on export.
                unresolved += 1
                continue
            matrix = matrices.get(node.source_index) if node else Matrix.Identity(4)
            skinned = arm_obj is not None and bool(mesh.bone_weights)
            bind = None
            if not skinned and node is not None:
                # Rigid meshes are authored in their own pivot space; the node
                # tail holds the mesh-to-node matrix the engine applies.  Without
                # it the cars of a map sink into the ground, the flyer membranes
                # land on the same side and a prop's parts drift apart.
                bind = rigid_bind_matrix(node, zwo.endian)
                if bind is not None:
                    matrix = matrix @ Matrix(bind)
            if frame is not None and not skinned:
                # Rigid parts (the flyer's wings) are authored in the same node
                # space as the skeleton, so they follow the same mesh transform
                # as the bones.  Skinned meshes stay in node space: the armature
                # modifier already conjugates their deformation by ``frame``.
                matrix = frame @ matrix
            obj = _build_mesh_object(mesh, node, matrix, collection,
                                     asset_root, options, report)
            obj["zwo_file"] = zwo_path
            obj["geo_file"] = geo_path
            if bind is not None:
                obj["zwo_rigid_bind"] = _flatten_matrix(Matrix(bind))
            # The placement Blender actually stored, read back so it carries the
            # same float precision the exporter will compare against.  A node
            # whose object still sits here is left untouched on export, which is
            # what keeps a no-op round trip byte identical (re-deriving the local
            # transform would lose the shear of a non-uniformly scaled parent).
            # Refreshed at the end of the import, once the depsgraph has settled.
            obj["zwo_rest_world"] = _flatten_matrix(obj.matrix_world)
            mesh_objects.append(obj)
            if frame is not None:
                # Remember the unanimated placement so rigid parts (wings) can be
                # carried into the mesh frame while the animation plays, and keep
                # the frame so the exporter can undo it when writing the node.
                if not skinned:
                    obj["zwo_frame"] = _flatten_matrix(frame)
            report["meshes"] += 1
            report["vertices"] += len(mesh.positions)
            report["faces"] += len(mesh.faces)
            if skinned:
                _bind_vertex_groups(obj, mesh, arm_obj)
                report["skinned"] += 1
            built.append((obj, mesh, node))
        report["bounds"] = len(mesh_nodes)
        if unresolved:
            report["warnings"].append(
                "%d instance record(s) point at geometry that is not in this "
                ".geo; their nodes were left empty" % unresolved)
        _tag_alternate_meshes(built, collection, report)

    if arm_obj is not None:
        _tag_helper_bones(arm_obj, collection, report)
        _fit_leaf_bones(arm_obj, collection, report)

    if options.get("lights", True):
        _build_lights(zwo, nodes, collection, report, zwo_path)
    if options.get("cameras", False):
        _build_cameras(zwo, nodes, collection, report, zwo_path)

    if mesh_objects:
        # Blender holds an object transform as location/rotation/scale, so a node
        # placement carrying shear (a rigid bind under a non-uniformly scaled
        # parent) is not representable: ``matrix_world`` keeps returning the
        # assigned matrix until the depsgraph runs, and the first evaluation
        # rebuilds it from ``matrix_basis`` with the shear gone.  Store the
        # placement Blender really holds, so an untouched node still compares
        # equal on export and keeps its original bytes (shear included).
        context.view_layer.update()
        for obj in mesh_objects:
            obj["zwo_rest_world"] = _flatten_matrix(obj.matrix_world)

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
