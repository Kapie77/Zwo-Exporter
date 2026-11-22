"""Export the actions imported from Final Exam `.ani` files as FBX.

Blender's FBX exporter is the interchange used by Unreal Engine (and Unity,
Maya, 3ds Max, ...).  Final Exam animations are stored as `.ani`, whose writer
is not implemented, so the supported route is: import `.ani` onto the armature
imported from the `.zwo`, then export the actions as FBX animation takes.

The exporter is driven through NLA strips so the result is the same in every
supported Blender version: Blender 4.0-5.3 ship the same FBX exporter and all
of them expose ``bake_anim_use_nla_strips``, while the newer
``export_animation_mode`` option is not available in any of them.  Each strip
is exported as its own FBX animation stack (one take), named after the action.
"""

import math
import os

import bpy


# ------------------------------------------------------------------ actions
def _action_paths(action):
    """Return the data paths animated by ``action`` (4.4+ slots included)."""
    paths = []
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        try:
            paths.extend(curve.data_path for curve in legacy)
        except (AttributeError, RuntimeError):
            paths = []
        if paths:
            return paths
    for layer in getattr(action, "layers", ()):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                paths.extend(curve.data_path for curve in bag.fcurves)
    return paths


def _bone_of(path):
    """Return the pose-bone name of a data path, or ``None``."""
    if not path.startswith('pose.bones["'):
        return None
    end = path.find('"]', 12)
    if end < 0:
        return None
    return path[12:end]


def _asset_key(value):
    if not value:
        return ""
    return os.path.normcase(os.path.abspath(value))


def armature_actions(armature):
    """Return the actions that animate ``armature``'s pose bones.

    Actions imported from a `.ani` carry the source `.zwo` in ``zwo_asset``; when
    some of those exist only they are exported, so animations imported for a
    different character in the same scene are not mixed in.  Actions created by
    hand in Blender are picked up through the same compatibility test.
    """
    bones = {bone.name for bone in armature.data.bones}
    compatible = []
    for action in bpy.data.actions:
        paths = _action_paths(action)
        if not paths:
            continue
        names = set()
        for path in paths:
            name = _bone_of(path)
            if name is None:
                names = None
                break
            names.add(name)
        if not names or not names <= bones:
            continue
        compatible.append(action)
    asset = _asset_key(armature.get("zwo_file"))
    if asset:
        tagged = [action for action in compatible
                  if _asset_key(action.get("zwo_asset")) == asset]
        if tagged:
            compatible = tagged
    compatible.sort(key=lambda action: action.name.lower())
    return compatible


def find_armature(context):
    """The active/selected armature, or the one deforming the active mesh."""
    active = context.view_layer.objects.active
    if active is not None and active.type == "ARMATURE":
        return active
    for obj in context.selected_objects:
        if obj.type == "ARMATURE":
            return obj
    if active is not None:
        for modifier in getattr(active, "modifiers", ()):
            if modifier.type == "ARMATURE" and modifier.object is not None:
                return modifier.object
        if active.parent is not None and active.parent.type == "ARMATURE":
            return active.parent
    return None


def _meshes_for(armature):
    meshes = []
    for obj in armature.children_recursive:
        if obj.type == "MESH" and obj not in meshes:
            meshes.append(obj)
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj in meshes:
            continue
        for modifier in obj.modifiers:
            if modifier.type == "ARMATURE" and modifier.object is armature:
                meshes.append(obj)
                break
    return meshes


# ------------------------------------------------------------------ export
def _fbx_kwargs(filepath, scale):
    """FBX exporter arguments, filtered to the ones this Blender exposes."""
    kwargs = {
        "filepath": filepath,
        "use_selection": True,
        "object_types": {"ARMATURE", "MESH"},
        "add_leaf_bones": False,
        "use_mesh_modifiers": True,
        "use_custom_props": False,
        "bake_anim": True,
        "bake_anim_use_all_bones": True,
        "bake_anim_use_nla_strips": True,
        "bake_anim_use_all_actions": False,
        "bake_anim_force_startend_keying": True,
        "bake_anim_step": 1.0,
        "bake_anim_simplify_factor": 0.0,
        "apply_unit_scale": True,
        "global_scale": scale,
        "axis_forward": "-Z",
        "axis_up": "Y",
    }
    props = bpy.ops.export_scene.fbx.get_rna_type().properties.keys()
    return {key: value for key, value in kwargs.items()
            if key == "filepath" or key in props}


def _stash(context, armature, actions):
    """Temporarily put one NLA strip per action on the armature."""
    data = armature.animation_data or armature.animation_data_create()
    previous = data.action
    previous_slot = getattr(data, "action_slot", None)
    data.action = None
    tracks = []
    for action in actions:
        start = float(action.frame_start)
        end = float(action.frame_end)
        if end <= start:
            end = start + 1.0
        track = data.nla_tracks.new()
        track.name = action.name
        strip = track.strips.new(action.name, int(math.floor(start)), action)
        strip.name = action.name
        strip.action_frame_start = start
        strip.action_frame_end = end
        strip.frame_start = 0.0
        strip.frame_end = max(1.0, float(math.ceil(end - start)))
        slots = getattr(action, "slots", None)
        if slots and hasattr(strip, "action_slot"):
            try:
                strip.action_slot = slots[0]
            except (TypeError, RuntimeError):
                pass
        tracks.append(track)
    return data, tracks, previous, previous_slot


def _restore(context, data, tracks, previous, previous_slot):
    for track in tracks:
        try:
            data.nla_tracks.remove(track)
        except (ReferenceError, RuntimeError):
            pass
    try:
        data.action = previous
        if previous_slot is not None and hasattr(data, "action_slot"):
            data.action_slot = previous_slot
    except (ReferenceError, RuntimeError, TypeError):
        pass


def _action_fps(actions, scene):
    """Frame rate the actions were imported at (native 30 fps)."""
    rates = {round(float(action["zwo_fps"]), 6) for action in actions
             if "zwo_fps" in action}
    if len(rates) == 1:
        return rates.pop()
    return scene.render.fps / scene.render.fps_base


def _set_scene_fps(scene, fps):
    scene.render.fps = int(round(fps))
    scene.render.fps_base = scene.render.fps / fps if fps else 1.0


def _export_one(context, armature, actions, filepath, include_meshes, scale):
    scene = context.scene
    view = context.view_layer
    previous_frame = scene.frame_current
    previous_fps = (scene.render.fps, scene.render.fps_base)
    previous_active = view.objects.active
    selected = [obj for obj in view.objects if obj.select_get()]
    fps = _action_fps(actions, scene)

    data, tracks, previous, previous_slot = _stash(context, armature, actions)
    try:
        for obj in view.objects:
            obj.select_set(False)
        armature.select_set(True)
        view.objects.active = armature
        if include_meshes:
            for mesh in _meshes_for(armature):
                try:
                    mesh.select_set(True)
                except RuntimeError:
                    pass
        # The FBX exporter converts frames to seconds with the scene rate, so
        # the original timing is written even if the scene was changed later.
        _set_scene_fps(scene, fps)
        scene.frame_set(0)
        result = bpy.ops.export_scene.fbx(**_fbx_kwargs(filepath, scale))
        if "FINISHED" not in result:
            raise RuntimeError("the FBX exporter returned %s" % result)
    finally:
        _restore(context, data, tracks, previous, previous_slot)
        scene.render.fps, scene.render.fps_base = previous_fps
        scene.frame_set(previous_frame)
        for obj in view.objects:
            obj.select_set(False)
        for obj in selected:
            try:
                obj.select_set(True)
            except ReferenceError:
                pass
        view.objects.active = previous_active


def _safe_name(name):
    cleaned = "".join("_" if char in '<>:"/\\|?*' else char for char in name)
    return cleaned.strip(" .") or "action"


def export_animations(context, filepath, options=None):
    """Write the armature's actions as one FBX (or one FBX per action)."""
    options = options or {}
    armature = find_armature(context)
    if armature is None:
        raise ValueError("select the armature imported from a .zwo")
    actions = armature_actions(armature)
    if not actions:
        raise ValueError("no .ani actions target %r (import a .ani first)"
                         % armature.name)

    include_meshes = bool(options.get("include_meshes", True))
    scale = float(options.get("scale", 1.0))
    mode = options.get("mode", "SINGLE")
    filepath = os.path.abspath(filepath)
    folder = os.path.dirname(filepath)
    if folder:
        os.makedirs(folder, exist_ok=True)

    if mode == "PER_ACTION":
        stem = os.path.splitext(os.path.basename(filepath))[0] or "animation"
        written = []
        for action in actions:
            target = os.path.join(
                folder, "%s_%s.fbx" % (stem, _safe_name(action.name)))
            _export_one(context, armature, [action], target, include_meshes,
                        scale)
            written.append(target)
        return {"files": written, "actions": [a.name for a in actions]}

    _export_one(context, armature, actions, filepath, include_meshes, scale)
    return {"files": [filepath], "actions": [a.name for a in actions]}
