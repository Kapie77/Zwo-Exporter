bl_info = {
    "name": "Zwo Exporter (Final Exam)",
    "author": "Kaplan",
    "version": (1, 0, 22),
    "blender": (4, 0, 0),
    "location": "File > Import-Export > Final Exam ZWO",
    "description": "Import and export Final Exam (.zwo / .geo / .hvt / .ani) assets",
    "warning": "Compressed .ani tracks (UCTK/VCTK) are not decoded",
    "doc_url": "",
    "category": "Import-Export",
}

import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper

from .zwo_exporter.importer import import_asset, import_animation
from .zwo_exporter.exporter import (
    ExportError, export_asset, objects_for_export)
from .fbx_export import export_animations

PACKAGE = __package__


# ------------------------------------------------------------------ import
class ZWO_OT_import(bpy.types.Operator, ImportHelper):
    """Import a Final Exam .zwo scene with its .geo and .hvt files"""

    bl_idname = "import_scene.fexam_zwo"
    bl_label = "Import ZWO"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".zwo"
    filter_glob: StringProperty(default="*.zwo", options={"HIDDEN"})

    use_textures: BoolProperty(
        name="Textures", default=True,
        description="Load .hvt textures through a generated DDS wrapper")
    use_lights: BoolProperty(name="Lights", default=True)
    use_cameras: BoolProperty(name="Cameras", default=False)
    fallback_root: StringProperty(
        name="Fallback data folder",
        subtype="DIR_PATH",
        default="",
        description=("Optional complete extraction of the same build, used for "
                     ".geo/.hvt when this .zwo folder only holds scenes. "
                     "Empty = use the add-on preference"))

    def execute(self, context):
        path = bpy.path.ensure_ext(self.filepath, ".zwo")
        if not os.path.isfile(path):
            self.report({"ERROR"}, "File not found: %s" % path)
            return {"CANCELLED"}
        options = {
            "textures": self.use_textures,
            "lights": self.use_lights,
            "cameras": self.use_cameras,
            "fallback_root": _fallback_root(context, self.fallback_root),
        }
        try:
            report = import_asset(context, path, options)
        except Exception as exc:                      # noqa: BLE001
            self.report({"ERROR"}, "Import failed: %s" % exc)
            return {"CANCELLED"}
        imported = report["meshes"] + report.get("cameras", 0) + report.get("lights", 0)
        if imported == 0:
            reason = "; ".join(report["warnings"]) or \
                "the .zwo contains no mesh, light or camera nodes"
            self.report({"ERROR"},
                        "%s: nothing imported - %s"
                        % (os.path.basename(path), reason))
            return {"CANCELLED"}
        self.report({"INFO"},
                    "%s: %d objects, %d vertices, %d faces, %d skinned"
                    % (os.path.basename(path), report["meshes"],
                       report["vertices"], report["faces"], report["skinned"]))
        for warning in report["warnings"]:
            print("ZWO import warning:", warning)
        return {"FINISHED"}


# ------------------------------------------------------------------ export
class ZWO_OT_import_animation(bpy.types.Operator, ImportHelper):
    """Import a Final Exam .ani action onto the imported armature"""

    bl_idname = "import_scene.fexam_zwo_anim"
    bl_label = "Import ZWO Animation"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".ani"
    filter_glob: StringProperty(default="*.ani", options={"HIDDEN"})

    fps: FloatProperty(
        name="Frame Rate", default=30.0, min=1.0, max=240.0,
        description="Animation keys are stored in milliseconds; 30 fps is the native rate")
    start_at_zero: BoolProperty(
        name="Start at frame 0", default=True,
        description="Shift the first key to frame 0 instead of its stored time")
    scene_range: BoolProperty(
        name="Set scene range", default=True,
        description="Match the scene frame range to the imported action")
    set_fps: BoolProperty(
        name="Set scene frame rate", default=True,
        description=("Match the scene frame rate to the imported action so "
                     "playback and FBX export keep the original timing"))

    def execute(self, context):
        path = bpy.path.ensure_ext(self.filepath, ".ani")
        if not os.path.isfile(path):
            self.report({"ERROR"}, "File not found: %s" % path)
            return {"CANCELLED"}
        options = {
            "fps": self.fps,
            "start_at_zero": self.start_at_zero,
            "scene_range": self.scene_range,
            "set_fps": self.set_fps,
        }
        try:
            report = import_animation(context, path, options=options)
        except Exception as exc:                      # noqa: BLE001
            self.report({"ERROR"}, "Animation import failed: %s" % exc)
            return {"CANCELLED"}
        self.report({"INFO"},
                    "%s: %d bones, %d objects, %.2f s"
                    % (os.path.basename(path), report["bones"],
                       report["objects"], report["duration"] / 1000.0))
        return {"FINISHED"}


# ------------------------------------------------------------------ export
def _asset_sources(objects):
    """Distinct source `.zwo` paths of the imported objects."""
    sources = []
    for obj in objects:
        path = obj.get("zwo_file")
        if not path:
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key not in sources:
            sources.append(key)
    return sources


def _resolve_asset(context):
    """The asset to export when the chosen path is not the imported one."""
    sources = _asset_sources(context.selected_objects)
    if not sources:
        sources = _asset_sources(context.scene.objects)
    return sources[0] if len(sources) == 1 else None


class ZWO_OT_export(bpy.types.Operator, ExportHelper):
    """Write back over the imported .zwo/.geo, or save a copy elsewhere"""

    bl_idname = "export_scene.fexam_zwo"
    bl_label = "Export ZWO"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".zwo"
    filter_glob: StringProperty(default="*.zwo", options={"HIDDEN"})

    use_selection: BoolProperty(
        name="Selected only", default=False,
        description="Export only selected objects")
    make_backup: BoolProperty(
        name="Keep .bak", default=True,
        description="Keep a .bak copy of the .zwo/.geo files being overwritten")
    copy_textures: BoolProperty(
        name="Copy textures", default=True,
        description=("When the file ends up outside the game data tree, copy the "
                     ".hvt textures it uses next to it so the copy imports with "
                     "textures"))

    def execute(self, context):
        path = os.path.abspath(bpy.path.ensure_ext(self.filepath, ".zwo"))
        candidates = context.selected_objects if self.use_selection else None
        objects = objects_for_export(candidates, path)
        if not objects:
            # A different path means "save a copy": pick the imported asset
            # (selection first) and let the exporter copy its pair there.
            source = _resolve_asset(context)
            if source is not None:
                objects = objects_for_export(candidates, source)
        if not objects:
            self.report({"ERROR"}, self._missing_message(context, path))
            return {"CANCELLED"}
        try:
            report = export_asset(objects, path, options={
                "backup": self.make_backup,
                "copy_textures": self.copy_textures,
                "fallback_root": _fallback_root(context),
            })
        except ExportError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:                      # noqa: BLE001
            self.report({"ERROR"}, "Export failed: %s" % exc)
            return {"CANCELLED"}
        written = ", ".join(os.path.basename(f) for f in report["files"])
        extra = ""
        if report.get("textures"):
            extra = ", %d textures copied" % report["textures"]
        if report.get("save_as"):
            self.report({"INFO"},
                        "Saved a copy to %s (%d objects, %d meshes rebuilt%s)"
                        % (written, report["nodes"], report["meshes"], extra))
        else:
            self.report({"INFO"}, "Wrote %s (%d objects, %d meshes rebuilt%s)"
                        % (written, report["nodes"], report["meshes"], extra))
        for warning in report["warnings"]:
            print("ZWO export warning:", warning)
        return {"FINISHED"}

    def _missing_message(self, context, path):
        sources = _asset_sources(context.selected_objects)
        if not sources:
            sources = _asset_sources(context.scene.objects)
        if not sources:
            return "No imported ZWO asset in this scene"
        if os.path.normcase(path) in sources:
            return ("Nothing to export: %s has no editable meshes"
                    % os.path.basename(path))
        if len(sources) == 1:
            return ("%s is not the imported asset; select its objects to save a "
                    "copy, or export over %s" % (path, sources[0]))
        names = ", ".join(os.path.basename(source) for source in sources[:4])
        if len(sources) > 4:
            names += ", ..."
        return ("%d assets are imported (%s); select the objects of the one to "
                "export, or pick its original path" % (len(sources), names))


class ZWO_OT_export_all(bpy.types.Operator):
    """Re-export every imported ZWO asset touched in this scene"""

    bl_idname = "export_scene.fexam_zwo_all"
    bl_label = "Export All Imported ZWO"

    make_backup: BoolProperty(name="Keep .bak", default=True)

    def execute(self, context):
        paths = []
        for obj in objects_for_export():
            path = os.path.abspath(obj["zwo_file"])
            if path not in paths:
                paths.append(path)
        if not paths:
            self.report({"ERROR"}, "Nothing to export")
            return {"CANCELLED"}
        total = 0
        for path in paths:
            objects = objects_for_export(None, path)
            try:
                report = export_asset(objects, path, options={
                    "backup": self.make_backup,
                    "copy_textures": True,
                    "fallback_root": _fallback_root(context),
                })
                total += report["meshes"]
            except Exception as exc:                  # noqa: BLE001
                self.report({"WARNING"}, "%s: %s" % (path, exc))
        self.report({"INFO"}, "Re-exported %d assets, %d meshes rebuilt"
                    % (len(paths), total))
        return {"FINISHED"}


class ZWO_OT_export_fbx(bpy.types.Operator, ExportHelper):
    """Export the imported .ani actions as FBX animation takes"""

    bl_idname = "export_scene.fexam_zwo_fbx"
    bl_label = "Export ZWO Animations (FBX)"
    bl_options = {"REGISTER"}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={"HIDDEN"})

    mode: EnumProperty(
        name="Files",
        items=(
            ("SINGLE", "One file, all actions",
             "Write every action as a separate take of a single FBX"),
            ("PER_ACTION", "One file per action",
             "Write <name>_<action>.fbx next to the chosen path"),
        ),
        default="SINGLE")
    include_meshes: BoolProperty(
        name="Include meshes", default=True,
        description=("Export the skinned meshes and skeleton together with the "
                     "animations, so Unreal imports a complete character"))
    scale: FloatProperty(
        name="Scale", default=1.0, min=0.001, soft_max=100.0,
        description="FBX global scale; Unreal converts the units on import")

    def execute(self, context):
        path = os.path.abspath(bpy.path.ensure_ext(self.filepath, ".fbx"))
        options = {
            "mode": self.mode,
            "include_meshes": self.include_meshes,
            "scale": self.scale,
        }
        try:
            report = export_animations(context, path, options)
        except Exception as exc:                      # noqa: BLE001
            self.report({"ERROR"}, "FBX export failed: %s" % exc)
            return {"CANCELLED"}
        files = ", ".join(os.path.basename(name) for name in report["files"][:4])
        if len(report["files"]) > 4:
            files += ", ..."
        self.report({"INFO"}, "Exported %d actions to %s"
                    % (len(report["actions"]), files))
        return {"FINISHED"}


# ------------------------------------------------------------------ panels
class ZWO_PT_panel(bpy.types.Panel):
    bl_label = "Final Exam ZWO"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ZWO"

    def draw(self, context):
        layout = self.layout
        layout.operator("import_scene.fexam_zwo", text="Import .zwo", icon="IMPORT")
        column = layout.column(align=True)
        column.operator("export_scene.fexam_zwo", text="Export .zwo", icon="EXPORT")
        column.operator("export_scene.fexam_zwo_all", text="Export all imported")
        column.label(text="Other path saves a copy", icon="INFO")

        box = layout.box()
        box.label(text="Animation", icon="ARMATURE_DATA")
        box.operator("import_scene.fexam_zwo_anim", text="Import .ani", icon="IMPORT")
        box.operator("export_scene.fexam_zwo_fbx", text="Export animations (.fbx)",
                     icon="EXPORT")
        animated = _active_armature(context)
        if animated is None:
            candidate = context.active_object
            if candidate is not None and candidate.animation_data is not None \
                    and candidate.animation_data.action is not None:
                animated = candidate
        if animated is not None:
            box.label(text=animated.name)
            action = animated.animation_data.action if animated.animation_data else None
            if action is not None:
                box.label(text="Action: %s" % action.name)

        obj = context.active_object
        if obj is not None and obj.get("zwo_file"):
            box = layout.box()
            box.label(text="Asset", icon="FILE")
            box.label(text=os.path.basename(obj["zwo_file"]))
            if not os.path.isfile(obj["zwo_file"]):
                row = box.row()
                row.alert = True
                row.label(text="Source .zwo is missing", icon="ERROR")
            if obj.get("zwo_node_name"):
                box.label(text="Node: %s" % obj["zwo_node_name"])
            if "zwo_mesh_index" in obj:
                box.label(text="Mesh %s, stride %s"
                          % (obj["zwo_mesh_index"], obj.get("zwo_mesh_stride")))
            if obj.get("zwo_bones"):
                box.label(text="%d bones" % len(obj["zwo_bones"].split(";")))

        layout.label(text="Zwo Exporter %s" % ".".join(str(v) for v in bl_info["version"]))


def _active_armature(context):
    obj = context.view_layer.objects.active
    if obj is not None and obj.type == "ARMATURE":
        return obj
    for candidate in context.selected_objects:
        if candidate.type == "ARMATURE":
            return candidate
    return None


# ------------------------------------------------------------ preferences
class ZWO_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or "Zwo Exporter"

    fallback_root: StringProperty(
        name="Fallback data folder",
        subtype="DIR_PATH",
        default="",
        description=("Complete extraction of the same build (the folder that "
                     "contains 'common' and 'maps'). Used for .geo/.hvt files "
                     "when the imported .zwo comes from a scene-only rip"))


def _fallback_root(context, override=""):
    """Operator override first, then the add-on preference."""
    if override:
        return override
    addon = context.preferences.addons.get(__package__ or "Zwo Exporter")
    prefs = addon.preferences if addon is not None else None
    return getattr(prefs, "fallback_root", "") if prefs is not None else ""


# ---------------------------------------------------------------- menus
def _menu_import(self, context):
    self.layout.operator("import_scene.fexam_zwo", text="Final Exam (.zwo)")
    self.layout.operator("import_scene.fexam_zwo_anim", text="Final Exam (.ani)")


def _menu_export(self, context):
    self.layout.operator("export_scene.fexam_zwo", text="Final Exam (.zwo)")
    self.layout.operator("export_scene.fexam_zwo_fbx",
                         text="Final Exam animations (.fbx)")


CLASSES = (
    ZWO_OT_import,
    ZWO_OT_import_animation,
    ZWO_OT_export,
    ZWO_OT_export_all,
    ZWO_OT_export_fbx,
    ZWO_PT_panel,
    ZWO_AddonPreferences,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
