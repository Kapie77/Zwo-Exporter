# Zwo Exporter (Final Exam)

A Blender 4.2 add-on for **Final Exam** (PC / Steam) scene assets. It imports
and exports the game's `.zwo` scene graphs together with their sibling `.geo`
geometry, `.hvt` textures and `.ani` animations.

The PC build is the primary target. The PS3 assets use the same record layout
with the opposite byte order and are handled by the same parsers, but PS3
animations have not been verified because no PS3 `.ani` files were available.

---

## Supported Versions

| Game                     |   PC   | Xbox 360 | PS3    |
|--------------------------|:------:|:------:|:-------:|
| Final Exam               |   ✅   |      |      |

## Features

| Format | Import | Export | Notes |
| ------ | ------ | ------ | ----- |
| `.zwo`  | yes | yes | scene hierarchy, transforms, node metadata |
| `.geo`  | yes | yes | meshes, UVs, normals, skin weights, material table |
| `.hvt`  | yes | no  | DXT/ARGB payload wrapped in a generated DDS |
| `.ani`  | yes | no  | skeletal and object animation (import only) |

* Imports all mesh data as Blender meshes, groups them in one collection per
  asset, and rebuilds lights and (optionally) cameras.
* Skinned characters get an armature whose rest bones exactly match the scene
  node world matrices, and vertex groups are built from the `.geo` weights.
* Textures are decoded to temporary DDS files and hooked into Principled BSDF
  materials.
* Export writes edits back into the original byte stream: untouched files
  round-trip byte-for-byte, changed meshes are rebuilt, and everything else
  (unknown tails, opaque records, checksums) is preserved verbatim.
* A `.bak` copy of the original `.zwo`/`.geo` pair is created before the first
  overwrite.

## Installation

1. Copy the `Zwo Exporter` folder (the one containing this README and
   `__init__.py`) into your Blender add-ons directory:

   ```
   %APPDATA%\Blender Foundation\Blender\4.2\scripts\addons\Zwo Exporter
   ```

   or zip the folder and use **Edit → Preferences → Add-ons → Install…**.
2. Enable **Zwo Exporter (Final Exam)** in the add-ons list.
3. The operators appear under **File → Import/Export** and in the 3D Viewport
   sidebar (`N` panel → **ZWO** tab).

## Usage

### Import a scene

**File → Import → Final Exam (.zwo)** and pick a `.zwo` file. The add-on looks
for the sibling `.geo` and `.hvt` files next to it and below the asset root
(the folder that contains `common` and `maps`).

Options:

* **Textures** – decode `.hvt` textures into materials.
* **Lights** – create point lights for `ZLIT` nodes.
* **Cameras** – create Blender cameras for `ZCAM` nodes.

### Import an animation

1. Import the character (or prop/camera) `.zwo` first.
2. Select the imported armature (or the animated object for object-only clips).
3. **File → Import → Final Exam (.ani)** and pick an `.ani`.

Options:

* **Frame Rate** – keys are stored in milliseconds; 30 fps is the native rate.
* **Start at frame 0** – shift the first key to frame 0 (clips often start at a
  non-zero time such as 4733 ms).
* **Set scene range** – match the scene frame range to the imported action.

The imported clip becomes an action on the armature; each animated non-skinned
object (camera, light, foliage, prop) gets its own action. Re-importing a clip
replaces the previous action on those IDs.

### Export

**File → Export → Final Exam (.zwo)** writes back over the pair the objects
were imported from. Use **Export all imported** to update every asset currently
loaded in the scene.

* Exports only objects tagged with their source `.zwo`; other objects are
  ignored.
* The exporter has no separate “save as” mode because the scene hierarchy and
  opaque record tails must stay aligned with the original file.

## Animation details

* Keys are **absolute local transforms**, not deltas. Rotation-only tracks
  (`NCTK`/`TCTK`) carry a constant translation in their track header;
  full-transform tracks (`RTFK`) carry per-key position, rotation and scale.
* Node targets are resolved with `CRC32(animation_path + "/" + node_name.lower())`,
  which was verified against the shipped assets.
* Bone animation is baked to pose-bone `matrix_basis` values, so it renders
  correctly regardless of edit-bone orientation.
* Skinned meshes are driven by the armature only; their object transform is
  left untouched to avoid double deformation.
* Camera/prop clips become object-level location/rotation/scale curves sampled
  from the animated node world matrix.

Not decoded:

* `UCTK` (compressed transform) and `VCTK` (compressed vector) tracks are
  parsed as raw blocks but not applied.
* `1LFK` single-float tracks are parsed but not mapped to a Blender property
  (their meaning is still unknown; values range from 0 to 1 and π/4).
* Animation export is not implemented.

## Export/round-trip guarantees

Tested against the shipped PC data:

* 353/353 `.zwo` and `.geo` files re-serialize byte-identically.
* 39/39 cache archives round-trip.
* 9,741 meshes, 3,160,928 vertices and 1,979,818 faces parse and rebuild.
* Untouched imports export byte-identically (no changes are invented).

## Limitations

* **PC first.** PS3 assets parse in the format layer but are not part of the
  validated Blender workflow. PS3 `.ani` files were not available to verify.
* **Checksums are not recomputed.** `.geo` block checksums are not CRC32 and
  their algorithm is unknown, so they are preserved byte-for-byte. This is fine
  for untouched blocks; rebuilt geometry keeps the original checksum value.
* **Material assignment is heuristic.** The format does not expose an explicit
  mesh→material index; materials are assigned by path/name matching. This works
  for maps and most props, but character parts (e.g. `flyer`) may need manual
  fixes.
* **Mesh/node association is name based.** Ambiguous names fall back to
  positional matching and are reported as warnings.
* **Instancing** is detected from shared geometry and kept on export, but the
  association is heuristic for duplicated node names.
* Unknown record tails and opaque nodes are preserved verbatim; they are never
  decoded into editable properties.
* Animation import only; there is no animation export or `.ani` writer.
* Extra animation-only hashes (bones that exist in an `.ani` but not in the
  `.zwo`) are skipped; their tracks are reported as unresolved.

## File layout

```
Zwo Exporter/
├─ __init__.py            add-on entry point, operators, panel, menus
├─ README.md
└─ zwo_exporter/
   ├─ __init__.py         side-effect free re-exports (no bpy)
   ├─ binio.py            endian-aware Reader/Writer
   ├─ zwo.py              .zwo scene graph parser/serializer
   ├─ geo.py              .geo geometry, materials, skin weights
   ├─ hvt.py              .hvt texture decoding to DDS
   ├─ ani.py              .ani parser and hashing helpers
   ├─ importer.py         Blender import (meshes, armatures, animation)
   └─ exporter.py         transform write-back and geometry patching
```

The modules in `zwo_exporter/` never import `bpy`, so the parsers can be tested
with plain Python.

## Byte order

* PC: little-endian integers with byte-reversed record tags (e.g. `NAON`).
* PS3: big-endian integers with literal tags.
* `.ani` track tags are matched in both spellings because the shipped files mix
  conventions.

## License

No license is bundled; treat the add-on as an unofficial community tool. Final
Exam and its assets belong to their respective rights holders.
