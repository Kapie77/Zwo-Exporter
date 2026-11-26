"""Blender-side exporter for Final Exam `.zwo` scenes.

The exporter never regenerates a file from scratch.  It re-parses the original
`.zwo`/`.geo`, copies the edited numbers back into the parsed structures and
lets the format layer re-emit the file.  Anything the add-on does not model
(unknown blocks, block checksums, the root stub, the `ZNAM` trailer) is carried
over byte for byte.
"""

import os
import shutil

import bpy

from .zwo import ZwoFile
from .geo import GeoFile


# ----------------------------------------------------------------- helpers
def _polygons_of(mesh):
    """Return faces as index triples, triangulating anything that is not a triangle."""
    loops = mesh.loops
    out = []
    for poly in mesh.polygons:
        if poly.loop_total == 3:
            out.append((loops[poly.loop_start].vertex_index,
                        loops[poly.loop_start + 1].vertex_index,
                        loops[poly.loop_start + 2].vertex_index))
        else:
            mesh.calc_loop_triangles()
            break
    if len(out) != len(mesh.polygons):
        out = []
        for tri in mesh.loop_triangles:
            out.append((loops[tri.loops[0]].vertex_index,
                        loops[tri.loops[1]].vertex_index,
                        loops[tri.loops[2]].vertex_index))
    return out


def _read_geometry(obj):
    """Pull positions/UVs/faces out of a Blender mesh in engine space."""
    mesh = obj.data
    # Both Blender and the engine's model space are Z-up, so vertices are copied
    # straight across.
    positions = [(v.co.x, v.co.y, v.co.z) for v in mesh.vertices]

    uvs = []
    if mesh.uv_layers:
        layer = mesh.uv_layers[0].data
        uvs = [(0.0, 0.0)] * len(mesh.vertices)
        for loop in mesh.loops:
            uv = layer[loop.index].uv
            uvs[loop.vertex_index] = (uv[0], 1.0 - uv[1])

    return positions, uvs, _polygons_of(mesh)


def _solid_faces(faces):
    """Set of real triangles, dropping degenerate ones (a repeated index).

    A triangle such as ``(8, 9, 9)`` draws nothing, so ``mesh.validate()``
    removes it when the importer builds the Blender mesh.
    """
    return {tuple(sorted(face)) for face in faces
            if len(face) == 3 and len(set(face)) == 3}


def _faces_equal(old_faces, new_faces):
    """Compare face lists, ignoring winding rotation and duplicate faces.

    Blender's ``mesh.validate()`` drops duplicated and degenerate triangles and
    may rotate the indices inside a triangle, neither of which is a real edit.
    """
    if old_faces == new_faces:
        return True
    return _solid_faces(old_faces) == _solid_faces(new_faces)


def _apply_geometry(obj, geo_mesh, report, dry_run=False):
    """Copy the Blender mesh into ``geo_mesh``; flag a rebuild only if it changed.

    With ``dry_run`` the comparison is made but nothing is written; the caller
    uses it to report edits on a record that cannot hold geometry (an instance).
    """
    positions, uvs, faces = _read_geometry(obj)

    if uvs and len(uvs) == len(geo_mesh.uvs):
        # ``mesh.validate()`` drops degenerate and duplicate triangles, so a
        # vertex used only by one of them has no loop left and therefore no UV.
        # Its stored UV is still valid data, so keep it instead of writing 0,0.
        used = {index for face in faces for index in face}
        for index in range(len(uvs)):
            if index not in used:
                uvs[index] = tuple(geo_mesh.uvs[index])

    changed = len(positions) != len(geo_mesh.positions)
    if not changed:
        for old, new in zip(geo_mesh.positions, positions):
            if abs(old[0] - new[0]) > 1e-5 or abs(old[1] - new[1]) > 1e-5 \
                    or abs(old[2] - new[2]) > 1e-5:
                changed = True
                break
    if not changed and not _faces_equal(geo_mesh.faces, faces):
        changed = True
    if not changed and uvs and len(uvs) == len(geo_mesh.uvs):
        for old, new in zip(geo_mesh.uvs, uvs):
            if abs(old[0] - new[0]) > 1e-4 or abs(old[1] - new[1]) > 1e-4:
                changed = True
                break

    if not changed or dry_run:
        return changed

    if len(positions) != len(geo_mesh.positions):
        report["warnings"].append(
            "vertex count changed on %r (%d -> %d); unknown vertex words are "
            "copied positionally" % (obj.name, len(geo_mesh.positions),
                                     len(positions)))
    geo_mesh.positions = positions
    geo_mesh.faces = [tuple(f) for f in faces]
    if uvs:
        geo_mesh.uvs = uvs
    geo_mesh.needs_rebuild = True
    return True


def _set_local(node, parent_world, world, force=False):
    """Write a Blender world matrix back into the node as a local transform."""
    from mathutils import Matrix

    local = parent_world.inverted() @ world
    if not force:
        current = Matrix(node.matrix())
        if all(abs(local[row][col] - current[row][col]) < 1e-5
               for row in range(4) for col in range(4)):
            return False
    loc, rot, scale = local.decompose()
    node.position = (loc.x, loc.y, loc.z)
    node.rotation = (rot.x, rot.y, rot.z, rot.w)
    node.scale = (scale.x, scale.y, scale.z)
    return True


def _matrix_close(a, b, epsilon=1e-5):
    return all(abs(a[row][col] - b[row][col]) < epsilon
               for row in range(4) for col in range(4))


def _node_space_world(obj):
    """Return an object's world matrix in `.zwo` node space.

    Rigid parts of a rig that has a mesh frame (the flyer's wings) are placed at
    ``frame @ node_world @ bind`` so they sit on the skinned mesh, where ``bind``
    is the mesh-to-node matrix stored in the node's tail.  The file stores the
    node world, so both are undone before comparing and writing.
    """
    from mathutils import Matrix

    world = obj.matrix_world
    bind = obj.get("zwo_rigid_bind")
    if bind:
        values = list(bind)
        world = world @ Matrix(tuple(tuple(values[row * 4:(row + 1) * 4])
                                     for row in range(4))).inverted()
    stored = obj.get("zwo_frame")
    if not stored:
        return world

    values = list(stored)
    frame = Matrix(tuple(tuple(values[row * 4:(row + 1) * 4])
                         for row in range(4)))
    return frame.inverted() @ world


def _stored_matrix(obj, key):
    """Return a 4x4 matrix stored by the importer as a flat list, or ``None``."""
    from mathutils import Matrix

    values = obj.get(key)
    if not values:
        return None
    values = list(values)
    if len(values) != 16:
        return None
    return Matrix(tuple(tuple(values[row * 4:(row + 1) * 4]) for row in range(4)))


def _update_node_transforms(zwo, objects, report):
    """Write every edited object's world transform back into its node.

    Nodes are visited parent-first so a moved parent also updates the local
    matrices of its edited children.  Objects whose world matrix did not change
    keep their parsed floats, which keeps a no-op export byte identical: the
    comparison uses the placement the importer stored after Blender evaluated
    it, not the raw node matrix, because a node under a non-uniformly scaled
    parent carries a shear that Blender cannot hold in ``matrix_basis``.
    """
    from mathutils import Matrix

    by_node = {}
    for obj in objects:
        index = obj.get("zwo_node")
        if index is not None:
            by_node[index] = obj
    touched = set()
    seen = set()

    def walk(node, parent_world, parent_changed):
        obj = by_node.get(node.source_index)
        changed = False
        if obj is not None:
            seen.add(node.source_index)
            rest = _stored_matrix(obj, "zwo_rest_world")
            moved = rest is None or not _matrix_close(obj.matrix_world, rest, 1e-6)
            if parent_changed or moved:
                world = _node_space_world(obj)
                _set_local(node, parent_world, world, force=True)
                touched.add(node.source_index)
                changed = True
        matrix = parent_world @ Matrix(node.matrix())
        for child in node.children:
            walk(child, matrix, changed)

    for child in zwo.children:
        walk(child, Matrix.Identity(4), False)
    return touched, seen


# -------------------------------------------------------------------- main
def objects_for_export(objects=None, file_path=None):
    """Collect the mesh objects that belong to one source asset."""
    candidates = objects if objects is not None else bpy.context.scene.objects
    out = []
    for obj in candidates:
        if obj.type != "MESH":
            continue
        path = obj.get("zwo_file")
        if not path:
            continue
        if file_path is not None:
            if os.path.normcase(os.path.abspath(path)) != \
                    os.path.normcase(os.path.abspath(file_path)):
                continue
        out.append(obj)
    return out


class ExportError(RuntimeError):
    """The scene cannot be written back to the requested file."""


#: Material keys the importer turns into image textures.
_TEXTURE_KEYS = ("diffusetexture", "texture", "blobshadingtexture",
                 "normaltexture", "speculartexture")


def _texture_names(geo):
    """The `.hvt` names a `.geo`'s materials point at, without repeats."""
    names = []
    for material in geo.materials:
        for key in _TEXTURE_KEYS:
            value = material.get(key)
            if isinstance(value, str) and value.strip() and value not in names:
                names.append(value)
    return names


def _copy_textures(geo, source_root, dest_root, fallback_root, folder, report):
    """Copy the `.hvt` files an asset needs next to a copy that cannot see them.

    A `.zwo`/`.geo` references textures by bare name, so a copy taken out of the
    game tree imports without textures.  Only loose destinations are filled in:
    when the file sits inside a data tree that already holds the textures (or
    when the fallback folder covers it) nothing is copied.
    """
    from .hvt import resolve_texture
    if os.path.normcase(os.path.abspath(dest_root)) != \
            os.path.normcase(os.path.abspath(folder)):
        return
    copied = 0
    missing = []
    for name in _texture_names(geo):
        if resolve_texture(name, dest_root):
            continue
        path = resolve_texture(name, source_root) if source_root else None
        if not path and fallback_root:
            path = resolve_texture(name, fallback_root)
        if not path:
            missing.append(name)
            continue
        target = os.path.join(folder, os.path.basename(path))
        if os.path.isfile(target):
            continue
        shutil.copy2(path, target)
        copied += 1
    report["textures"] = copied
    if missing:
        report["warnings"].append(
            "%d texture(s) not found next to the copy: %s%s"
            % (len(missing), ", ".join(missing[:6]),
               ", ..." if len(missing) > 6 else ""))


def _sibling(path, extension):
    """Return ``<stem><extension>`` next to ``path`` (case-insensitive)."""
    base = os.path.splitext(path)[0]
    direct = base + extension
    if os.path.isfile(direct):
        return direct
    folder = os.path.dirname(path)
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


def _has_exported_nodes(path, objects):
    """True when ``path`` still holds every node the objects came from."""
    names = {obj["zwo_node_name"] for obj in objects if obj.get("zwo_node_name")}
    if not names:
        return True
    try:
        zwo = ZwoFile.load(path)
    except Exception:                                  # noqa: BLE001
        return False
    return names <= {node.name for node in zwo.walk()}


def _recover_source(source_zwo, source_geo, fallback_root, objects):
    """Find the `.zwo` a scene was imported from after it moved on disk.

    Only places the asset is known to have lived in are tried: next to the
    imported `.geo`, in the folder of the recorded `.zwo` (any letter case) and,
    for a scene-only rip, the same relative path under the fallback data
    folder.  A candidate must still contain the nodes the scene was built from,
    so an unrelated file is never patched by accident.
    """
    candidates = []
    if source_geo:
        candidates.append(os.path.splitext(source_geo)[0] + ".zwo")
    if source_zwo:
        candidates.append(source_zwo)
        if fallback_root:
            from .importer import asset_root_for
            try:
                relative = os.path.relpath(source_zwo,
                                           asset_root_for(source_zwo))
            except ValueError:
                relative = None
            if relative and not relative.startswith(".."):
                candidates.append(os.path.join(fallback_root, relative))
    for candidate in candidates:
        found = _sibling(candidate, ".zwo")
        if found and _has_exported_nodes(found, objects):
            return found, _sibling(found, ".geo")
    return None, None


def export_asset(objects, zwo_path, geo_path=None, options=None):
    """Write the edited objects back over ``zwo_path`` (and its `.geo`).

    ``zwo_path`` is normally the file the objects were imported from, which is
    overwritten in place.  Any other path saves a copy: the source `.zwo` and
    `.geo` are copied there first and then patched with the scene edits, so the
    original asset is never touched.
    """
    options = options or {}
    report = {"meshes": 0, "nodes": 0, "files": [], "textures": 0,
              "warnings": []}

    zwo_path = os.path.abspath(zwo_path)
    if geo_path is None:
        geo_path = os.path.splitext(zwo_path)[0] + ".geo"
    geo_path = os.path.abspath(geo_path)

    source_zwo = None
    source_geo = None
    for obj in objects:
        if source_zwo is None and obj.get("zwo_file"):
            source_zwo = os.path.abspath(obj["zwo_file"])
        if source_geo is None and obj.get("geo_file"):
            source_geo = os.path.abspath(obj["geo_file"])

    # The scene keeps pointing at the file it was imported from.  If that file
    # was moved, renamed or deleted, look for it again before writing anything.
    if source_zwo and not os.path.isfile(source_zwo):
        recovered_zwo, recovered_geo = _recover_source(
            source_zwo, source_geo, options.get("fallback_root"), objects)
        if recovered_zwo is None:
            raise ExportError(
                "the .zwo this scene was imported from no longer exists: %s. "
                "Restore that file or import the asset again, then export."
                % source_zwo)
        report["warnings"].append(
            "the imported .zwo was missing; used %s instead" % recovered_zwo)
        source_zwo, source_geo = recovered_zwo, recovered_geo

    if options.get("backup", True):
        for path in (zwo_path, geo_path):
            if os.path.isfile(path) and not os.path.isfile(path + ".bak"):
                shutil.copy2(path, path + ".bak")

    if source_zwo and os.path.normcase(source_zwo) != os.path.normcase(zwo_path):
        folder = os.path.dirname(zwo_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        shutil.copy2(source_zwo, zwo_path)
        if source_geo and os.path.isfile(source_geo):
            shutil.copy2(source_geo, geo_path)
        report["save_as"] = source_zwo
    elif not os.path.isfile(zwo_path):
        raise ExportError(
            "no imported .zwo to read from and %s does not exist; import the "
            "asset before exporting" % zwo_path)

    zwo = ZwoFile.load(zwo_path)
    geo = None
    if os.path.isfile(geo_path):
        geo = GeoFile.load(geo_path)
    elif any(obj.get("zwo_mesh_index") is not None for obj in objects):
        report["warnings"].append(
            "no .geo next to the .zwo; geometry edits were not written")

    # A copy taken out of the game tree keeps working only if it can still see
    # its textures, which are referenced by bare name.
    if geo is not None and options.get("copy_textures", True):
        from .importer import asset_root_for
        folder = os.path.dirname(zwo_path)
        _copy_textures(geo,
                       asset_root_for(source_zwo or source_geo or zwo_path),
                       asset_root_for(zwo_path),
                       options.get("fallback_root"),
                       folder, report)
    touched_nodes, seen_nodes = _update_node_transforms(zwo, objects, report)
    for obj in objects:
        if obj.get("zwo_node") is not None and \
                obj["zwo_node"] not in seen_nodes:
            report["warnings"].append("missing node for %r" % obj.name)

    applied = set()
    for obj in objects:
        mesh_index = obj.get("zwo_mesh_index")
        if geo is None or mesh_index is None:
            continue
        meshes = geo.meshes
        if not (0 <= mesh_index < len(meshes)):
            report["warnings"].append("missing mesh for %r" % obj.name)
            continue
        if mesh_index in applied:
            report["warnings"].append(
                "%r shares geometry with another instance; kept the first "
                "edited copy" % obj.name)
            continue
        geo_mesh = meshes[mesh_index]
        if geo_mesh.is_instance:
            # The record holds only the placement's lightmap UVs and draw
            # calls; the vertices belong to the mesh it points at.  Writing an
            # edit here would silently drop it, so say so instead.
            if _apply_geometry(obj, geo_mesh, report, dry_run=True):
                report["warnings"].append(
                    "%r is an instance of %r; its geometry is shared and the "
                    "edit was not written (edit the source mesh instead)"
                    % (obj.name, geo_mesh.source.model_path.rsplit("/", 1)[-1]
                       if geo_mesh.source is not None else "a missing mesh"))
            continue
        if _apply_geometry(obj, geo_mesh, report):
            report["meshes"] += 1
            applied.add(mesh_index)

    report["nodes"] = len(touched_nodes)

    if geo is not None:
        geo.save(geo_path)
        report["files"].append(geo_path)
    zwo.save(zwo_path)
    report["files"].append(zwo_path)
    return report
