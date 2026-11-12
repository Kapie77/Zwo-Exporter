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


def _faces_equal(old_faces, new_faces):
    """Compare face lists, ignoring winding rotation and duplicate faces.

    Blender's ``mesh.validate()`` drops duplicated triangles and may rotate the
    indices inside a triangle, neither of which is a real edit.
    """
    if old_faces == new_faces:
        return True
    if len(new_faces) > len(old_faces):
        return False
    return ({tuple(sorted(face)) for face in new_faces} ==
            {tuple(sorted(face)) for face in old_faces})


def _apply_geometry(obj, geo_mesh, report):
    """Copy the Blender mesh into ``geo_mesh``; flag a rebuild only if it changed."""
    positions, uvs, faces = _read_geometry(obj)

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

    if not changed:
        return False

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


def _world_matrices(zwo):
    from mathutils import Matrix

    out = {}

    def walk(node, parent_world):
        matrix = parent_world @ Matrix(node.matrix())
        out[node.source_index] = matrix
        for child in node.children:
            walk(child, matrix)

    for child in zwo.children:
        walk(child, Matrix.Identity(4))
    return out


def _matrix_close(a, b, epsilon=1e-5):
    return all(abs(a[row][col] - b[row][col]) < epsilon
               for row in range(4) for col in range(4))


def _update_node_transforms(zwo, objects, report):
    """Write every edited object's world transform back into its node.

    Nodes are visited parent-first so a moved parent also updates the local
    matrices of its edited children.  Objects whose world matrix did not change
    keep their parsed floats, which keeps a no-op export byte identical.
    """
    from mathutils import Matrix

    original = _world_matrices(zwo)
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
            world = obj.matrix_world
            if parent_changed or not _matrix_close(
                    world, original[node.source_index]):
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


def export_asset(objects, zwo_path, geo_path=None, options=None):
    """Write the edited objects back over ``zwo_path`` (and its `.geo`)."""
    options = options or {}
    report = {"meshes": 0, "nodes": 0, "files": [], "warnings": []}

    zwo_path = os.path.abspath(zwo_path)
    if geo_path is None:
        geo_path = os.path.splitext(zwo_path)[0] + ".geo"

    zwo = ZwoFile.load(zwo_path)
    geo = None
    if os.path.isfile(geo_path):
        geo = GeoFile.load(geo_path)

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
        if _apply_geometry(obj, meshes[mesh_index], report):
            report["meshes"] += 1
            applied.add(mesh_index)

    report["nodes"] = len(touched_nodes)

    if options.get("backup", True):
        for path in (zwo_path, geo_path):
            if os.path.isfile(path) and not os.path.isfile(path + ".bak"):
                shutil.copy2(path, path + ".bak")

    if geo is not None:
        geo.save(geo_path)
        report["files"].append(geo_path)
    zwo.save(zwo_path)
    report["files"].append(zwo_path)
    return report
