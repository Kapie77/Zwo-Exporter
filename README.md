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

## Quick Guide

* **Install the entire `Zwo Exporter` folder** (the outer folder, which contains
  `__init__.py` and `README.md`). When creating a ZIP file for installation, the ZIP file must
  contain the structure `Zwo Exporter\__init__.py` + `Zwo Exporter\zwo_exporter\`.
  Zip the **entire main** folder—never just the inner `zwo_exporter` folder—since it is
  merely the formatting layer and is not an add-on.
* **Import the `.zwo` file from the complete extracted game tree.** The `.geo`
  (geometry) file must be **in the same folder and have the same name** as the `.zwo`
  file (`basic.zwo` → `basic.geo`). A `.zwo` file copied on its own to another folder cannot
  generate meshes.
* For **textures**, the corresponding `.hvt` file must also be extracted.
* For **animations**, the `.ani` files must also be extracted (they are
  usually located in `...\anims\` or `...\anims\all\`). First import the character's `.zwo` file,
  select the armature, and only then import the `.ani` file.
* Keep the `data\common\...` and `data\maps\...` structure intact; do not move
  individual files outside of it.

---

## Features

| Format | Import | Export | Notes |
| ------ | ------ | ------ | ----- |
| `.zwo`  | yes | yes | scene hierarchy, transforms, node metadata |
| `.geo`  | yes | yes | meshes, UVs, normals, skin weights, material table, draw-call material ranges |
| `.hvt`  | yes | no  | DXT1/DXT3/DXT5 and uncompressed ARGB/XRGB wrapped in a generated DDS |
| `.ani`  | yes | no  | skeletal and object animation (import only) |

* Imports all mesh data as Blender meshes, groups them in one collection per
  asset, and rebuilds lights and (optionally) cameras.
* Materials come from the `.geo` palette by **exact hash**: every `ZSEQ` draw
  call stores `CRC32(material path)`, so each face range gets the material the
  game really uses. Meshes split across several draw calls get several material
  slots (1,230 meshes in the shipped PC data are multi-material).
* Skinned characters get an armature whose rest bones exactly match the scene
  node world matrices, and vertex groups are built from the `.geo` weights.
* Textures are decoded to temporary DDS files and hooked into Principled BSDF
  materials. Every `.hvt` format in the shipped data is supported (DXT1, DXT3,
  DXT5 and the uncompressed ARGB/XRGB variants).
* Per-vertex colours stored in the `.geo` are imported as the `zwo_color`
  attribute. Characters multiply the full RGB into the base colour, which is
  what gives schoolkids their skin, backpack and clothing tints; without it
  those areas look white.
* Monster materials follow the shipped `shadow0/monster.fx` shader exactly. Its
  vertex colour is not a tint but a **mask**: the red channel blends between the
  lit diffuse texture (`red = 1`) and the unlit `blobshadingtexture` ramp
  (`red = 0`). The importer rebuilds that as `base = diffuse × red` plus
  `emission = ramp × 1.5 × (1 − red)`, so the body keeps its texture while the
  eyes, mouth and organic patches keep the green fresnel glow they have in the
  game. Meshes whose diffuse texture is the neutral `_onlycolordiffuse`
  placeholder (tentacles, nests, plants) are coloured entirely by the ramp.
* `usealpha` / `alpha` are treated as a **cut-out** mask (alpha thresholded and
  dithered) instead of smooth transparency, so characters such as Cassy no
  longer look washed out. Only `opacity < 1` or `additive` keep real blending.
* Export writes edits back into the original byte stream: untouched files
  round-trip byte-for-byte, changed meshes are rebuilt, and everything else
  (unknown tails, opaque records, checksums) is preserved verbatim.
* A `.bak` copy of the original `.zwo`/`.geo` pair is created before the first
  overwrite.

## Materials

Materials are rebuilt from the `.geo` palette. The properties that drive the
look are mapped like this:

| `.geo` property | Blender |
| --------------- | ------- |
| `diffusetexture` | Base Color texture |
| `diffusecolor` | fallback Base Color |
| `normaltexture` + `normalmapboost` | Normal Map |
| `.geo` vertex colour (characters) | `zwo_color` attribute, multiplied into Base Color |
| `.geo` vertex colour red (monsters) | mask: Base Color = diffuse × red, Emission = ramp × (1 − red) |
| `blobshadingtexture` | unlit facing ramp (monster green, boss red, ...) |
| `glossiness` / `specularmultiplier` | Roughness / Specular |
| `usealpha` / `alpha` | cut-out alpha (thresholded, dithered) |
| `opacity` / `additive` | smooth alpha blending |
| `twosided` | backface culling off |

Characters usually have one material and one texture; the per-part colours
(skin, backpack, uniform) come from the vertex colours stored in the `.geo`,
not from separate materials. Monster hides are dark on purpose — the shipped
`basic_diff`/`tank_diff`/`jumper_diff` atlases are almost black (mean RGB around
40), and the green cast the Hitter has in screenshots comes from the game's
green scene lighting, not from a bright texture.

## Installation

1. Copy the **whole `Zwo Exporter` folder** (the outer folder containing this
   README and `__init__.py`, NOT the inner `zwo_exporter` package) into your
   Blender add-ons directory:

   ```
   %APPDATA%\Blender Foundation\Blender\4.2\scripts\addons\Zwo Exporter
   ```

   or zip **the whole main folder** and use
   **Edit → Preferences → Add-ons → Install…**. The ZIP must contain:

   ```
   Zwo Exporter\__init__.py
   Zwo Exporter\README.md
   Zwo Exporter\zwo_exporter\...
   ```

   Do **not** zip the inner `zwo_exporter` folder on its own: it has no
   `bl_info`/`register()` and Blender will not recognize it as an add-on.
2. Enable **Zwo Exporter (Final Exam)** in the add-ons list (newly installed
   add-ons start disabled; this is normal).
3. The operators appear under **File → Import/Export** and in the 3D Viewport
   sidebar (`N` panel → **ZWO** tab).

## Usage

### Import a scene

**File → Import → Final Exam (.zwo)** and pick a `.zwo` file **from the fully
extracted game data tree** (keep the original `data\common\...` /
`data\maps\...` structure). The add-on needs the sibling files next to the
`.zwo`:

* `basic.geo` – geometry (required for meshes);
* `basic.hvt` – textures;
* `basic.ani` or files under `...\anims\` – animations.

Importing a `.zwo` that was copied alone into another folder cannot create
meshes, because its `.geo` is missing. In that case the add-on now aborts with
an explicit error naming the expected `.geo` path instead of leaving only an
armature.

Options:

* **Textures** – decode `.hvt` textures into materials.
* **Lights** – create point lights for `ZLIT` nodes.
* **Cameras** – create Blender cameras for `ZCAM` nodes.

### Import an animation

Animations are separate `.ani` files; they must be extracted from the game as
well (usually under the character's `...\anims\` or `...\anims\all\` folder).

1. Import the character (or prop/camera) `.zwo` first.
2. Select the imported armature (or the animated object for object-only clips).
3. **File → Import → Final Exam (.ani)** and pick an `.ani` file.

Each `.ani` is a separate clip (walk, run, attack, ...); import as many as you
need. Re-importing the same clip replaces the previous action on those IDs.

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
* **Materials are exact, with a small fallback.** Each `ZSEQ` draw call carries
  `CRC32(material.path.lower())`, which is resolved against the `.geo` palette
  and assigned per face range. This reproduces the game's material layout on all
  353 shipped `.geo` files (11,763 / 11,763 draw calls). Only 24 meshes have no
  usable draw-call list and still fall back to path/name matching.
* **The boost/glow state is not reproduced.** `characterglow`, `boostedversion`
  and the green "boosted" portraits are runtime shader/lighting effects. The
  importer reproduces the stored `blobshadingtexture` ramp as unlit emission,
  but it does not switch materials into their boosted state.
* **Monster brightness depends on the scene.** `monster.fx` computes
  `diffuse × (lighting + 0.5)`, and the game's green lamps are bright. The
  importer leaves the diffuse texture at its stored value and lets Blender's
  lights supply the rest, so a neutral render looks darker and browner than an
  in-game screenshot.
* **The grayscale `shadingtexture` toon ramp is not applied.** It is a lighting
  ramp (`character_shading`, black → white); Blender lights the models instead.
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
├─ LICENSE.txt
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

## Troubleshooting

**Only an armature appears / “0 objects, 0 vertices, 0 faces, 0 skinned”**

The `.geo` file was not found. The importer needs the `.geo` **next to** the
`.zwo`, with the same base name (`basic.zwo` → `basic.geo`). This happens when
only the `.zwo` is copied out of the extracted game tree. Import the file from
its original folder (below `common`/`maps`) instead of a scratch copy. The
add-on now aborts with the exact expected path and does not leave an orphan
armature behind.

**The add-on is listed but does nothing / cannot be enabled**

Make sure you installed the folder that contains `__init__.py` and this README
(`Zwo Exporter`), not the inner `zwo_exporter` package. When creating a ZIP,
zip the **whole main folder** so the archive contains
`Zwo Exporter\__init__.py` + `Zwo Exporter\zwo_exporter\`. The inner folder is
only the format layer and has no `bl_info`, so Blender does not recognize it as
an add-on.

**Importing `.ani` does nothing / “no node matches this animation”**

The animation targets the armature/objects of a previously imported `.zwo`, and
the `.ani` files must have been extracted from the game too (typically under
`...\anims\` or `...\anims\all\`). Import the asset first, select its armature
(or animated object) and then import the `.ani`. Re-importing a clip replaces
the action on those IDs. Animations are not embedded in the `.zwo`.

**Textures are pink / untextured**

Textures are read from the sibling `.hvt`. If it is missing, geometry still
imports but materials stay untextured. Check the console for the exact texture
warnings.

## License

GPL-3.0 (see `LICENSE.txt`). Treat the add-on as an unofficial community tool;
Final Exam and its assets belong to their respective rights holders.
