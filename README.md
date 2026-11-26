# Zwo Exporter (Final Exam)

A Blender add-on for **Final Exam** (PC / Steam, PS3 and Xbox 360) scene
assets. It imports and exports the game's `.zwo` scene graphs together with
their sibling `.geo` geometry, `.hvt` textures and `.ani` animations.

The PC build is the primary target. The PS3 and Xbox 360 builds use the same
record layout, the opposite byte order and a packed vertex format; all three are
handled by the same parsers and have been validated against the full shipped
data (see [PS3 build](#ps3-build) and [Xbox 360 build](#xbox-360-build)).

---

## Supported Versions

| Game                     |   PC   | Xbox 360 | PS3    |
|--------------------------|:------:|:------:|:-------:|
| Final Exam               |   ✅   |   ✅   |   ✅   |

## Blender compatibility

The add-on requires **Blender 4.0 or newer** (`bl_info["blender"] = (4, 0, 0)`)
and is tested headless on every minor series up to 5.3:

| Series | Tested build | PC | PS3 | Xbox 360 |
|--------|--------------|:--:|:---:|:--------:|
| 4.0    | 4.0.2        | ✅ | ✅  | ✅       |
| 4.1    | 4.1.1        | ✅ | ✅  | ✅       |
| 4.2    | 4.2.23 (LTS) | ✅ | ✅  | ✅       |
| 4.3    | 4.3.2        | ✅ | ✅  | ✅       |
| 4.4    | 4.4.3        | ✅ | ✅  | ✅       |
| 4.5    | 4.5.16 (LTS) | ✅ | ✅  | ✅       |
| 5.0    | 5.0.1        | ✅ | ✅  | ✅       |
| 5.1    | 5.1.2        | ✅ | ✅  | ✅       |
| 5.2    | 5.2.4        | ✅ | ✅  | ✅       |
| 5.3    | 5.3.0        | ✅ | ✅  | ✅       |

Each build was verified with the same suite: import of a PC map, a PC/PS3/Xbox
360 character with textures, a byte-identical no-op re-export, animation import
(including evaluation of the posed armature) and 32-bit texture equality.

Blender 5.0 removed the legacy `Action.fcurves` API. Animation import detects the
slotted-action API and writes the curves through an action slot, so the same
`.ani` files produce the same actions and the same evaluated poses in every
supported version.

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
* **Scene-only rips.** Some archives unpack as scenes only (`data.hvp\...` holds
  just the `.zwo` files). Point **Fallback data folder** — in the add-on
  preferences or in the import dialog — at a complete extraction of the **same
  build**: the importer then finds the missing `.geo` and `.hvt` at the same
  relative path there. Exporting back into the scene-only folder rewrites only
  the `.zwo` and warns that the geometry was not written; exporting to a
  different folder copies the `.geo` found in the fallback folder, producing a
  complete pair.
* **Exporting.** Picking the imported path rewrites the original `.zwo`/`.geo`;
  picking another folder or name saves a patched copy there and leaves the
  original untouched.
* For **textures**, the corresponding `.hvt` file must also be extracted.
* For **animations**, the `.ani` files must also be extracted (they are
  usually located in `...\anims\` or `...\anims\all\`). First import the character's `.zwo` file,
  select the armature, and only then import the `.ani` file.
* To take the animations to **Unreal Engine** (or Unity, Maya, ...), select the
  armature and use **Export animations (.fbx)**; see *Export animations (FBX)*
  below.
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
| `.fbx`  | no  | yes | actions imported from `.ani` as FBX takes (Unreal Engine, Unity, Maya) |

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
* Every rigid mesh node stores its own **mesh-to-node matrix** in the first
  48 bytes of its tail (the tail length varies: 81 bytes on props, 104–133 on
  the map nodes). Vertices are authored in the part's own pivot space, so the
  importer places the object at `node_world · bind`; without it the cars of
  `map_00_a` sink into the road, the flyer's membranes land on the same side
  and a prop's parts drift apart (the basket's bottom sits 7 units below its
  top). The matrix is used on every rigid mesh — skinned meshes keep the bone
  data in their tail and are placed by the armature — and only when it is a
  clean orthonormal transform with a positive determinant; a few FX tails carry
  a stretched frame instead. 21,599 of the 21,752 nodes in the shipped maps
  carry a valid matrix. The exporter divides it back out, so a no-op export is
  still byte-identical.
* **Placed copies share their geometry.** The level builder stores a model once
  and writes a second, much smaller record for every further placement: it has
  `0` where the block id goes, `CRC32(model path)` in the next slot, a running
  record id, and only the per-placement lightmap UVs and the draw calls. Every
  placement still owns a `ZENT` node, so a map that imports only the full
  records loses more than half of its props — `map_00_a` had 249 of its 483
  meshes (bins, barrels, cans, lamp posts, vents, cars, trees, foliage, decals),
  `map_01_a` 132 of 273. The importer now resolves the hash against the mesh
  table of the same `.geo` (10,388 instance records in the shipped data) and
  reuses that geometry; the record's own `ZSEQ` still decides its material, so a
  fog sheet can reuse another one's plane under `fogsheet_rays` while its source
  uses `fogsheet_rays2`. Editing an instance's vertices in Blender is reported
  as a warning instead of being written, because its record has nowhere to put
  them — edit the source mesh instead. The record bytes are preserved, so a
  no-op export stays byte-identical.
* `diffusecolor` is the material **tint** the shaders multiply into the base
  colour (`diffuse · texture · vertex colour`); `main_static`, `decal`,
  `character` and `collectable` all declare the `DiffuseColor` constant. Level
  art recolours one shared atlas with it: `road_marks` and `decals_diff` are
  white and take their yellow/coloured look from the tint, the basket's and the
  blender's `_onlycolordiffuse` bodies take their whole colour from it, and the
  map's boundary plane (`black`) is `(0, 0, 0)` — without the tint it rendered
  as a white wall across the level. A missing key means no tint; the 0.8 grey
  parsed default is not a colour.
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
* **Cube-map reflections (`envmaptexture`/`useenvmap`, `env1`–`env7`) and the
  runtime pickup glow (`glowtexture`) are not reproduced: the env maps are DDS
  cube maps that the importer does not bind, and the glow ramp is driven by the
  game, not by the weapon surface.
* **Baked lightmaps are not applied.** Map materials carry
  `lightmaptexturea` (e.g. `lm_map_00_a`) and the level is lit from it in the
  game. The importer does not sample it, so maps are lit by Blender lights
  instead and render brighter and flatter than the game. Geometry, materials
  and placement are unaffected.
* Export writes edits back into the original byte stream: untouched files
  round-trip byte-for-byte, changed meshes are rebuilt, and everything else
  (unknown tails, opaque records, checksums) is preserved verbatim.
* **Sheared node placements survive a no-op export.** Blender keeps an object
  transform as location/rotation/scale, so a node whose rigid `bind` matrix
  lands under a non-uniformly scaled parent cannot hold its shear in
  `matrix_basis`; the first depsgraph evaluation rebuilds `matrix_world` without
  it (a few map doors shift by ~1e-2). The importer therefore records the
  placement Blender really holds after the scene settles, and the exporter
  compares against that: a node nobody moved keeps its original floats (shear
  included), and only a real edit is written back. All 23 shipped `map_01`
  chunks and `map_00_a` export byte-identically.
* **Degenerate triangles do not trigger a rebuild.** A few map records contain a
  zero-area triangle (a repeated vertex index, e.g. `(8, 9, 9)` in the
  `shop06`/`shop07`/`shop08` of `map_01_n`, or `(380, 380, 381)` in the PS3
  `boss_fallingrocks`), and some records store the same triangle twice.
  `mesh.validate()` drops both on import, and the exporter ignores degenerate and
  duplicated faces when deciding whether the geometry changed, so the record
  keeps its bytes. Vertices that were used only by a dropped triangle keep their
  stored UV, because Blender leaves them without a loop. If the mesh is edited
  for real, the rebuilt record carries the reduced face count and the import
  warning (`face count changed during import (13 -> 12)`) explains the
  difference.
* A `.bak` copy of the original `.zwo`/`.geo` pair is created before the first
  overwrite.

## PS3 build

The PS3 extraction is a drop-in replacement for the PC one: same paths, same
record layout, same node trees, materials and geometry. Import a PS3 `.zwo` from
its extracted folder exactly like a PC one; nothing has to be converted.

* **Byte order.** Integers are big-endian and record tags read literally
  (`ZROO`, `NAON`), where the PC build stores little-endian numbers and
  byte-reversed tags (`OORZ`, `NOAN`). `.zwo`, `.geo`, `.ani` and `.hvt` are all
  detected automatically.
* **Vertex format.** PS3 vertices are packed: stride 32 (static) / 48 (skinned)
  instead of the PC 56 / 72. Positions, packed 11-11-10 tangent frame, colour,
  UVs and the four skin weights/bone indices decode to the same values as the
  PC build. The UV pair is one word with `u` in the high half on PS3 (the PC
  stores `u` in the low half); both orders are handled.
* **Animations.** PS3 `.ani` files are big-endian and import into identical
  F-curves as their PC counterparts (same bones, tracks, durations and values).
* **Textures.** PS3 ships 1,337 `.hvt` files against the PC's 1,983 (platform
  UI art differs, e.g. `buttons_ps3` vs `buttons_360`). Shared textures decode
  to the same payloads; a few dozen UI textures are stored as DXT5/DXT1 on PS3
  where the PC keeps uncompressed ARGB, which the existing decoder handles.

Verified against the full PS3 extraction:

* 353/353 `.zwo`, 353/353 `.geo` and 1,953/1,953 `.ani` parse and re-serialize
  byte-identically.
* 353 `.zwo` node trees, 5,092 materials, 20,129 meshes, 4,707,334 vertices and
  2,952,153 faces are equivalent to the PC build (same names, transforms,
  material properties, positions, UVs and triangles).
* All 353 assets import and export unchanged byte-for-byte, with world matrices
  and bounding boxes matching the PC build exactly.
* Animation import produces identical actions for the sampled clips
  (`bottom_walk`, `chainsaw_run`, `9mm_aim_front_shoot`).

## Xbox 360 build

The Xbox 360 extraction is also a drop-in replacement: same paths, same record
layout, same node trees, materials and geometry as the PS3 build. Import an X360
`.zwo` from its extracted folder like any other; nothing has to be converted.

* **Byte order and vertex format.** Like PS3, integers are big-endian with
  literal tags and vertices use the packed 32/48-byte layout, so the PS3 vertex
  decoder serves the X360 data unchanged. `.zwo`, `.geo` and `.ani` parse and
  re-serialize byte-identically.
* **Textures.** X360 `.hvt` files keep every mip level (and every cube face) in
  a single `DATA` chunk, stored in the Xenos tiled layout: 32×32-block macro
  tiles (`Tiled2D`), the mip chain following the base level, and the smallest
  levels packed into one tile. The DXT payload is also byte-swapped in 16-bit
  words relative to the PS3/PC blocks, and uncompressed 32-bit pixels are stored
  `A,R,G,B`. The decoder undoes all three and emits the same DDS as the PC
  build (the three `*_360` UI atlases decode to the PC bytes exactly).
* **Cube maps.** `env*` textures are six-face cube maps in a level-major
  layout. The importer keeps face 0, matching how the PS3 container (which
  repeats the mip indices once per face) is collapsed.

Verified against the full X360 extraction:

* 353/353 `.zwo`, 353/353 `.geo` and 1,953/1,953 `.ani` parse and re-serialize
  byte-identically (49,134 records, 49,050 tracks).
* 20,129 meshes, 4,707,334 vertices, 2,952,153 faces, 5,092 materials and all
  node trees are identical to the PS3 build (same positions, UVs, triangles,
  colours, weights and bone names).
* All 353 assets import and export unchanged byte-for-byte.
* 1,337 `.hvt` textures decode; every texture shared with the PS3/PC builds is
  byte-identical (including the first face of the 14 cube maps), and the
  X360-only `buttons_360`/`buttons_big_360` atlases decode to the PC DDS.

## Materials

Materials are rebuilt from the `.geo` palette. The properties that drive the
look are mapped like this:

| `.geo` property | Blender |
| --------------- | ------- |
| `diffusetexture` | Base Color texture |
| `diffusecolor` | Base Color tint, multiplied with the texture and the vertex colour |
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
   %APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\Zwo Exporter
   ```

   (e.g. `4.2`, `4.5` or `5.3`). In Blender 4.2+ the legacy add-on is still
   installed through **Edit → Preferences → Add-ons → Install…**; the
   *Extensions* tab only lists extensions that ship a `blender_manifest.toml`.

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
* **Set scene frame rate** – match the scene frame rate to the clip (30 fps
  native). Without this the clip would play at the wrong speed in a scene set to
  24 fps, and FBX export would carry the wrong duration.

The imported clip becomes an action on the armature; each animated non-skinned
object (camera, light, foliage, prop) gets its own action. Re-importing a clip
replaces the previous action on those IDs.

### Export

**File → Export → Final Exam (.zwo)** writes the scene back to the asset the
objects were imported from. Pick the original `.zwo` path to overwrite it in
place, or **pick any other folder or name to save a copy**: the source `.zwo`
and `.geo` are copied there and then patched with your edits, leaving the
original untouched. Use **Export all imported** to update every asset currently
loaded in the scene.

* Exports only objects tagged with their source `.zwo`; other objects are
  ignored.
* A copy keeps the scene hierarchy and opaque record tails aligned with the
  original, so it imports exactly like the source asset.
* Textures are referenced by name, not by path. **Copy textures** (on by
  default) writes the `.hvt` files the asset uses next to a copy that lands
  outside the game data tree, so the copy imports with its textures anywhere.
  Nothing is copied when the destination tree — or a **Fallback data folder** —
  already holds them.
* When several assets are imported, select the objects of the one to export so
  the add-on knows which pair to copy.
* The scene always points at the file it was imported from. If that `.zwo` was
  moved, renamed or deleted, the add-on looks for it again next to the imported
  `.geo`, in the same folder (any letter case) and, when a **Fallback data
  folder** is set, at the same relative path there. It only accepts a file that
  still holds the nodes the scene was built from. When nothing matches it stops
  with a clear message instead of failing halfway through a write; the panel
  also flags *Source .zwo is missing*. Import the asset again in that case.

### Export animations (FBX)

Select the **armature** imported from the `.zwo`, then use **Export
animations (.fbx)** in the *ZWO* panel or **File → Export → Final Exam
animations (.fbx)**. Every action that animates that armature is written as an
FBX animation take; Unreal Engine imports each take as one animation sequence.

Options:

* **Files** – *One file, all actions* (recommended: a single FBX containing one
  take per action) or *One file per action* (`<name>_<action>.fbx`).
* **Include meshes** – also write the skinned meshes and the skeleton, so the
  FBX imports into Unreal as a complete character. Turn it off for
  animation-only files that target an existing skeleton.
* **Scale** – FBX global scale (default `1.0`). Blender units are metres and
  Unreal converts them on import, so leave it at `1.0` unless your pipeline
  expects centimetres.

Notes:

* Only the armature's actions are exported; object-level tracks (doors,
  cameras, foliage, props) are not part of the FBX.
* Actions imported from a `.ani` are tagged with their source `.zwo`. When the
  scene contains several characters, only the actions belonging to the selected
  armature's asset are written.
* The animation is baked with one key per frame and every bone keyed, at the
  frame rate stored on the action when the `.ani` was imported (30 fps native).
  That is what Unreal's retargeting and compression expect, and it keeps the
  original duration even if the scene rate was changed afterwards.
* The skeleton takes the Blender armature name (`zwo_armature` by default).
  Rename the armature object before exporting if you want a different skeleton
  name in Unreal.
* FBX is the safest interchange for Unreal Engine (and Unity, Maya, 3ds Max).
  glTF also works in UE5, but FBX remains the standard for skeletal meshes and
  animations.
* Re-importing an FBX into the same Blender scene creates a second armature and,
  with **Include meshes**, a second copy of the mesh. To check a take, switch the
  armature's action in the Action editor instead of importing the file again.
* Writing `.ani` back is not supported: the game's animation blocks are not
  re-serialised, and compressed `UCTK`/`VCTK` tracks are not even decoded.

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
* Writing `.ani` back is not implemented; use the FBX export instead.

## Export/round-trip guarantees

Tested against the shipped PC data:

* 353/353 `.zwo` and `.geo` files re-serialize byte-identically.
* 39/39 cache archives round-trip.
* 9,741 meshes, 3,160,928 vertices and 1,979,818 faces parse and rebuild.
* Untouched imports export byte-identically (no changes are invented).
* Exporting to another path writes a copy whose `.zwo`/`.geo` are byte-identical
  to the source when nothing was edited; the original is left untouched.

Tested against the shipped PS3 data:

* 353/353 `.zwo`, 353/353 `.geo` and 1,953/1,953 `.ani` files re-serialize
  byte-identically.
* All 353 assets import and export unchanged byte-for-byte, including the
  20,038 meshes with degenerate or duplicated triangles that `mesh.validate()`
  drops.
* 1,953 animations parse (49,134 records, 49,050 tracks); the 292 `.ani` files
  under `skins\` carry no tracks in either build.

## Limitations

* **PC first.** The PC build is the primary target and the one with the
  longest test history; the PS3 and Xbox 360 builds are validated against their
  full extractions (see [PS3 build](#ps3-build) and
  [Xbox 360 build](#xbox-360-build)) but have seen less in-game comparison.
* **Checksums are not recomputed.** `.geo` block checksums are not CRC32 and
  their algorithm is unknown, so they are preserved byte-for-byte. This is fine
  for untouched blocks; rebuilt geometry keeps the original checksum value.
* **Materials are exact.** Each `ZSEQ` draw call carries
  `CRC32(material.path.lower())`, which is resolved against the `.geo` palette
  and assigned per face range. This reproduces the game's material layout on all
  353 shipped `.geo` files: all 20,038 meshes and 22,484 draw calls resolve.
  (A path/name fallback still exists for a record without a usable draw-call
  list; no shipped mesh needs it.)
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
* **Most placements are instanced, a few are unresolved.** 10,388 `.geo`
  records place a model that is stored once elsewhere in the same file; those
  resolve exactly by `CRC32(model_path)`. 91 records (0.9 %) hash a model that
  is in neither the same `.geo` nor any sibling one (`JoeLWing`,
  `Object05`, the `b_ceilinglamp01_ON*` lamps of `map_01_t`, some
  `b_park_BG*` backdrop planes), so their nodes are imported empty and the
  import reports one aggregated warning. The records themselves are preserved,
  so a no-op export is unaffected.
* **A mesh's second vertex/face buffer is a shadow copy.** Each static mesh
  repeats its faces against a position-only vertex buffer (stride 12, welded
  positions) with the same draw calls; it is not extra geometry, so the importer
  reads only the first buffer. 385 meshes declare one to ten more faces in the
  record header than the first face buffer can index; those triangles point at
  vertices outside the vertex buffer, so they are dropped (the geometry and the
  draw calls still agree with each other).
* Unknown record tails and opaque nodes are preserved verbatim; they are never
  decoded into editable properties.
* There is no `.ani` writer. Animations go out as FBX
  ([Export animations (FBX)](#export-animations-fbx)); the game's animation
  blocks are not re-serialised.
* Extra animation-only hashes (bones that exist in an `.ani` but not in the
  `.zwo`) are skipped; their tracks are reported as unresolved.

## File layout

```
Zwo Exporter/
├─ __init__.py            add-on entry point, operators, panel, menus
├─ fbx_export.py          actions -> FBX takes (Unreal Engine, Unity, Maya)
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
* PS3 and Xbox 360: big-endian integers with literal tags.
* `.ani` track tags are matched in both spellings because the shipped files mix
  conventions.
* The packed UV word also swaps halves: PS3/X360 store `u` in the high half, PC
  in the low half.
* `.hvt` DXT blocks are byte-swapped in 16-bit words on X360; uncompressed
  32-bit pixels are `A,R,G,B` on X360 and `B,G,R,A` on PC.

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
