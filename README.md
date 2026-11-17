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
* Skinned characters get an armature whose rest bones match the scene node
  world matrices (or the bind data, for the few posed rigs described below),
  and vertex groups are built from the `.geo` weights.
  A `.zwo` bone runs along the node's local **+X** while Blender bones run along
  **+Y**, so the rest matrix includes a fixed −90° roll; without it every rig's
  bones stuck out sideways, ~90° away from the limbs they belong to.
* Every skinned `ZBON` tail ends with a 3×4 **inverse bind matrix**
  (`bind = (M · world)⁻¹`, the last 48 bytes of the 109-byte tail). `M` is the
  constant transform between the skeleton's node space and the mesh, so the
  importer applies it to the **armature object** instead of guessing a yaw.
  For the flyer family that is `Rz(−90°)` **plus** a translation
  `(0, 0.157, −0.069)`, and for `joe` a translation
  `(0.048, 0.073, 0.021)`; the old yaw-only fit ignored the translation and
  left every bone ~0.17 units off the body (forearms, hands and stings ended
  up outside the mesh). Rigid parts (the wings) receive the same transform, and
  the exporter undoes it before writing node transforms, so the files still
  round-trip byte-for-byte.
* A few rigs keep a **posed** node tree instead of the bind pose: their nodes
  carry their own scale (0.78–1.10) and rotation, so the per-bone implied `M`
  disagrees and no single object transform can line the skeleton up with the
  mesh - the bones end up rotated and floating beside the body (`fatwilly`,
  `scientist`, `nathan`; spread 0.14–1.89 against the 0.005 tolerance). For
  those the armature is built from the bind data instead: `bind⁻¹` is exactly
  the rest matrix of the bone in mesh space, so every joint lands back inside
  the character (`fatwilly` 10/28 → 24/25 bones inside the skin, `scientist`
  13/30 → 28/30). Animations then take the rest product from the bone itself
  instead of the node chain.
* If a rig has no bind tails, the bind data is not rigid, or the per-bone
  matrices already agree (spread ≤ 0.02), the importer falls back to the old
  strict yaw detector (2D Procrustes fit of bone heads to weighted vertex
  centroids: ≥6 bones, |yaw| 85–95°, residual < 0.3). Two shipped rigs
  (`puck_blob`, `spit_blob`) only need the `Rz(−90°)` rotation.
* Bone **tails** follow the first child bone only when that child sits on the
  bone's own axis (within 45°); otherwise the bone keeps a short stub. Some
  rigs hang a branch (a mouth, a second arm, a thigh) far to the side of its
  parent, and using that distance used to spear a long bone straight out of the
  head or the neck. Bones with no child to measure (the last bone of a finger,
  claw, toe or wing) have no length in the file either, so the fallback is a
  fixed 0.12 - much longer than the spitter's claws, which left the tips poking
  through the hand. Once the meshes exist those tails are clipped to the last
  point that is no further outside the skin than the head already is, so a tail
  never spears out of the character. Tails are display-only: rest matrices,
  weights and the exported data are unaffected.
* Bones that no vertex uses are marked non-deforming and moved to a hidden
  **`Helpers (no weights)`** bone collection; bones that do deform stay in
  **`Deform`**. `spitter`, for example, shares `jumper`'s four-armed skeleton
  but its `.geo` only weights two arms, so the second pair would otherwise
  float around the body. Root bones stay visible because they are the handle
  for moving the whole character. Enable the collection in the armature
  properties to show them again.
* Alternative versions of one part are parked in a hidden **`Alternates
  (hidden)`** collection instead of overlapping each other. `holga` ships two
  faces, `holga_head` (neutral, 228 vertices) and `holga_headangry` (screaming,
  393 vertices), both rigidly bound to `JoeHead` and occupying the same space;
  the game swaps them to change her expression, so importing both makes the
  face z-fight and read as a broken texture. Parts that are rigidly bound to
  the *same* single bone, share a name prefix and overlap by at least 90% of
  the smaller bounding box are treated as variants: the shortest name stays
  visible and the others move to the hidden collection, which can be enabled
  in the outliner. Hidden variants are still part of the export set, so a
  no-op export keeps every mesh.
* Rigid parts of a transformed rig (the flyer's wings) are placed with the same
  mesh transform as the bones, so they stay attached to the skinned body; when
  an `.ani` is imported their animated node matrices are multiplied by that
  transform again.
* A rigid part of a rig also stores its own **mesh-to-node matrix** in the first
  48 bytes of the 81-byte `ZENT` tail. The membranes of `flyer` and
  `flyer_catcher` need it: their geometry is authored in part space, so without
  it both membranes land on the same side of the body and the left one floats
  0.23 units off its bone. The importer places those parts at
  `M · world · bind` and the exporter divides it back out, so a no-op export is
  still byte-identical. The same 81-byte tail appears on props and FX
  (`blood_puddle`, `poison_zone`, `blender`, …) where the numbers are *not* a
  mesh bind — they move decals or turn a particle strip horizontal — so the
  matrix is only used for a rigid mesh below a `ZBON` node, and only when it is
  a clean orthonormal transform with a positive determinant.
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
* Backface culling follows the `twosided` flag. The `.geo` writes `twosided`
  only when it is false, meaning the material culls its back faces; when the
  key is absent the material is two-sided (461 materials carry the flag, 4,631
  omit it). Blender draws both sides by default, which exposed the inside of
  one-sided shells: the schoolkids' hair showed the blood decal its inner faces
  point at, and the duplicate inner layer z-fought with the outer hair. The
  importer now enables `use_backface_culling` when the flag is present, so the
  hair is clean; it is display-only and never touches the exported bytes.
* Specular follows the shipped shaders: `SpecularColor × SpecularMultiplier ×
  SpecularTexture`, gated by `UseSpecular`/`UseSpecularTexture`. The multiplier
  becomes **Specular IOR Level**, a non-white `specularcolor` becomes **Specular
  Tint**, and `speculartexture` is sampled as a Non-Color mask multiplied into
  the level. That is what the weapons need: `9mm_spec`/`batte_spec` are white on
  the metal and black on the grip, so the wooden and rubber parts stop being as
  glossy as the slide. `crowbar`, `grenade` and `chainsaw`'s teeth point
  `speculartexture` at their diffuse while `usespeculartexture` is `False`, so
  the flag is what decides. `character.fx` declares no specular parameters at
  all, and character materials carry `usespecular=False`, so they end up with no
  specular term.
* Cube-map reflections (`envmaptexture`/`useenvmap`, `env1`–`env7`) and the
  runtime pickup glow (`glowtexture`) are not reproduced: the env maps are DDS
  cube maps that the importer does not bind, and the glow ramp is driven by the
  game, not by the weapon surface.
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
| `glossiness` | Roughness |
| `specularmultiplier` + `speculartexture` + `specularcolor` | Specular IOR Level / Specular Tint |
| `usespecular` / `usespeculartexture` | gates the specular term and its mask |
| `usealpha` / `alpha` | cut-out alpha (thresholded, dithered) |
| `opacity` / `additive` | smooth alpha blending |
| `twosided` (only written as `False`) | backface culling on; absent means two-sided |

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
  correctly regardless of edit-bone orientation. The basis is conjugated by the
  same −90° bone-axis roll that the rest uses, so the deformation
  (`pose @ rest⁻¹`) is bit-for-bit what the engine would produce (verified to
  3·10⁻⁵ on `jumper`), and the −90° rest roll is purely visual.
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
