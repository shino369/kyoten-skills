# Proven Live2D Rig Patterns (Zero External Generative Editing)

These patterns eliminate the most common failure modes when turning illustrations or game character sprites into production-ready Live2D Cubism v5 models with See-through and PSD2Live, incorporating techniques validated against hand-rigged professional `.moc3` models.

## 1. Two-Pass See-through + Native-Pixel Re-projection

See-through outputs a 1280×1280 square padded PSD whose diffusion-reconstructed pixels are softer than original artist lineart and occasionally drop dark hats or shoes.

- **Two-pass decomposition (multi-part characters):**
  1. Composite an armless body image and run See-through once to split `back hair`, `neck`, `legwear`, `footwear`, `topwear`, and `neckwear`.
  2. Crop tightly around the head and run See-through a second time at 1280px so `face`, `front hair`, `nose`, `ears`, `eyebrow`, `eyewhite`, `irides`, and `eyelash` keep full pixel detail.
- **Native-pixel re-projection (`reproject_visible_pixels`):**
  - Always map layers back into the source artwork's full native canvas resolution via `unpad_params` + `map_bbox` + `paste_onto_canvas` (done automatically by `recover-single`).
  - Process layers front-to-back, accumulating an `occluder_mask` of opaque pixels from layers in front.
  - Wherever a layer is visible in the original artwork and not covered by `binary_dilation(occluder_mask, 3)`, blend crisp original artist RGB back in with a Gaussian feather (`sigma=1.5`).
  - Behind occluders, keep See-through's built-in hidden-area completion pixels so head turns and hair sway never expose blank seams.
- **General cleanup rules:**
  - **Prevent double-nose shadow drift:** Exclude a dilated nose mask (`iterations=12`) from `head_face` re-projection so See-through's clean skin stays behind `head_nose`, while re-projecting crisp native pixels onto `head_nose`.
  - **Separate clothing attachments from back hair:** If See-through merges collar ribbons or shoulder-strap accessories into `body_back_hair`, transfer those components onto `body_topwear` and smooth-fill the back hair behind them so clothing accessories move with the torso instead of hair.
  - **Preserve native outer contour for accessory-OFF hair:** Never chop a character's back-hair outer contour at an arbitrary Y coordinate when building a bare-head variant. Keep 100% of the original outer silhouette and ink line, and replace only interior shadow pixels (`binary_erosion(silhouette, 4)`) with the bare-crown colour profile.

## 2. Working with Extracted Multi-Part Sprites (Case 1B)

Game sprite rips (Unity/KiriKiri/Naninovel) often lack pivot metadata and mix different crop sizes or pixel scales across parts.

1. **Calibrating placement & scale when pivots/offsets are missing:**
   - Measure the neutral composite (or inspect the face region on the base head sprite) to compute the exact `(left, top, width, height)` bounding boxes of the eye sockets, mouth anchor, and arm attachment points.
   - If any rendered reference of the assembled character exists (character sheet, game screenshot, atlas preview), register each part against it with masked multi-scale template matching (for example OpenCV `matchTemplate` with `TM_SQDIFF` and the part's alpha as the mask, over a range of scales). The best match gives each part's position and scale in the reference; divide by the common scale to get native canvas coordinates.
   - Expect every part of one rig to register at the **same** scale, so parts compose 1:1 at native size. Do not rescale a part (for example eyes or mouth) by eye-balled socket width; rescale only when its registered scale clearly differs from the rest. Tiny parts (mouths of a few dozen pixels) can be too small to register on a full-body reference: register them on a close-up (bust/face) image instead and express their offset relative to the head.
2. **Aligning variant sprites of different sizes:**
   - Expression variants (`Eyes_Cry.png`, `Eyes_Angry.png`, raised arms) frequently have tighter or wider tight-cropped bounding boxes than the neutral sprite (for example, teardrops extending downward or raised brows extending upward).
   - Never align variants blindly by top-left `(x, y)` or image center. Instead, anchor them by a shared feature (e.g., upper-lash/brow landmark or normalized cross-correlation against the neutral eye/shoulder mask) and compute each variant's canvas `rect` relative to that anchor.
3. **Whole-eye sprite tagging pitfalls:**
   - Do **not** tag a fused single-layer eye sprite (containing sclera + iris + lash in one PNG) as `["eye", "iris"]` or `["eye", "white"]`: PSD2Live's blink deformer squashes eyelids while leaving a single `iris` mesh visible behind the closed lid.
   - For **live open eyes**, use the 1280px head-pass See-through layers (`eyewhite`, `irides`, `eyelash`) with native-pixel re-projection so gaze tracking, clipping, and blinking work independently.
   - For **static closed-eye expression sprites** (Smile, Gloom), tag them as `["object"]`, bind them to the face contour deformer (`顔デフォーマ`), and add a 2D opacity gate (Pattern 3 below).
   - When splitting bilateral eye/brow sprites with `split-eyes`, verify stderr `warnings`: on three-quarter faces, `split_eyes_and_brows` uses per-side relative geometry above each eye anchor and warns whenever a side has no separate brow component.

## 3. Live Open-Eye Expressions + Blink-Immune Closed-Eye Expressions

Replacing eyes with flat static expression sprites freezes eyeball gaze tracking and blink.

- **Open-eye expressions (Flushed, Pensive, Cry, Angry, Surprised):**
  - Keep `ParamEyeExpr = 0` so the separated live `eyewhite` / `irides` / `eyelash` layers remain active with full gaze and blink.
  - Never lock `ParamEyeLOpen` / `ParamEyeROpen` inside open-eye `exp3` files.
  - Author a custom `ParamEyeShape` axis on the `DeformEyes` deformer (e.g., `-1.0` narrowed angry, `+1.0` softened sad/cry, `+2.0` wide surprised) and combine with rigged eyebrows (`ParamBrowLY/RY`, `Angle`, `Form`).
  - For tears (`Cry`), extract only the tear drops/streams by subtracting the neutral eye sprite and add them as a dedicated `["object"]` toggle layer (`ParamCryTears`) bound to the face contour deformer.
- **Closed-eye expressions (Smile, Gloom):**
  - Assign dedicated `ParamEyeExpr` switch IDs (`1`, `2`, ...) to the artist's closed-eye variants (tagged `["object"]` and bound to the face contour deformer so `face/fitted` does not double-warp them).
  - Use `split_eyes_and_brows` on the neutral closed-eye sprite for live auto-blink (`eye_close_l/r`, tagged `["eye", "closed", "left"|"right"]`, with `eye.see-through` and `brow.see-through` disabled).
  - **Critical blink gate:** Apply 2D `keyform_apply` opacity keys on `eye_close_l/r` across `(ParamEyeLOpen/ROpen, ParamEyeExpr)` so `eye_close_l/r` has `opacity: 1.0` ONLY at `(Open=0.0, ParamEyeExpr=0.0)` and `opacity: 0.0` at `ParamEyeExpr >= 0.5`. Without this gate, EyeBlink or Blink motions flash neutral closed lids on top of Smile/Gloom.

## 4. Method A: Single Unified Live Rigged Mouth

Swapping static mouth sprites per expression causes squashing at `ParamMouthOpenY=0`, duplicated oval rings from `mouth.outline`, and jarring position jumps during lip-sync.

- Disable the `mouth.outline` modifier in `post_rig.json`.
- Keep **one** live mouth layer (`mouth_open`, tagged `["mouth", "open"]`, using a clean open mouth sprite).
- Drive every expression's resting and talking mouth shape purely through Live2D mouth parameters:
  - `ParamMouthForm` (`+1.0` smile, `-0.85` cry, `-0.95` angry, `-0.35` pensive)
  - `ParamMouthPuckerWiden` (`+0.25` shy/flushed, `-0.45` surprised)
  - `ParamMouthShrug` (`+0.25` angry)
- Result: identical mouth anchor position across all expressions, continuous closed-to-open lip-sync on every expression, and zero double-line artefacts.
- _(Upgrade path for studio polish: hand-rigged models split the mouth into 3+ meshes—upper lip line, lower lip line, and clipped mouth cavity—for crisper closed-lip contours and distinct A/I/U/E/O vowel silhouettes.)_

## 5. 3D Headwear Parallax & Clipped Cast Shadows

Two PSD2Live pitfalls break standard headwear rigging:

1. Binding headwear to `DeformHairFrontFollow` (`前髪 頭部追従`) stretches the far edge outward and pushes the crown peak opposite to the head turn, reading like a backwards 2D sticker.
2. Calling `author_axis` directly on parent-owned `ParamAngleX` / `ParamAngleY` is silently ignored by PSD2Live (`forms: 1`).

The clean, native Cubism v5 solution (upgraded with the 3-layer cast-shadow architecture used in pro models):

1. **Split into 2–3 depth layers along natural seams:**
   - **Rear silhouette / outer band** with a smooth solid-fabric underfill extending ~4–5% inward behind the front rim so Z-parallax never opens a gap.
   - **Front detail rim** (e.g., scalloped lace or front brim).
   - **Optional soft cast-shadow layer** (tinted, Gaussian-blurred silhouette copy placed beneath the headwear or bangs/collar with reduced opacity or multiply blending) so shadows glide across the hair, forehead, and chest when hair/headwear moves.
   - Separate hanging left/right ribbons for independent pendulum physics (prefer short-ID `author_axes` + `physics_modifiers` over `swing_put` in PSD2Live 3.8.4 so ribbon output IDs never truncate in `.moc3`).
2. **Rebind all headwear meshes** off `DeformHairFrontFollow` onto the clean head container (`頭部コンテナ` / `DeformHeadContainer`).
3. **Author subtle convex 3D parallax on new dedicated parameters** (`ParamHdYaw` `[-1, 1]` and `ParamHdPitch` `[-1, 1]`):
   - Use a 5×3 `landmarks` grid where the front rim shifts ~8–10 px farther toward the turn than the rear band, while the far side compresses gently inward and the crown peak stays highest.
   - Sync ribbon and hairclip translations analytically from the outer band's temple anchor delta (`dx_px / child_width`, `dy_px / child_height`).
4. **Drive `ParamHdYaw` / `ParamHdPitch` 1:1 from `ParamAngleX` / `ParamAngleY` via native Cubism `physics3` angle-input modifiers:**

```json
{
  "id": "PhysicsHdYaw",
  "name": "Headwear 3D Yaw",
  "layer_ids": [
    "headwear_outer",
    "headwear_inner",
    "headwear_ribbon_r",
    "headwear_ribbon_l"
  ],
  "inputs": [{ "parameter": "ParamAngleX", "weight": 100.0, "type": "angle" }],
  "outputs": [{ "parameter": "ParamHdYaw", "vertex": 1, "scale": 1.273 }],
  "segments": [
    { "length": 2.0, "mobility": 0.98, "delay": 0.22, "acceleration": 3.0 }
  ],
  "normalization": {
    "position": { "min": -1.0, "default": 0.0, "max": 1.0 },
    "angle": { "min": -45.0, "default": 0.0, "max": 45.0 }
  }
}
```

Because it compiles into standard `.physics3.json` with short parameter IDs (`<64` bytes), the 3D parallax works identically in `.moc3`, VTube Studio, Cubism Viewer, and WebGL.

## 6. Pro Physics Upgrades: Blink-Driven Eye Bounce & Paired XY Hair Chains

Studying high-quality hand-rigged Live2D models reveals two physics layers that dramatically elevate liveliness with minimal configuration:

1. **Blink-driven eye bounce (`ParamEyeBounce`):**
   - Drive a subtle secondary bounce parameter (`[-1, 1]`) from `ParamEyeLOpen` (50%) + `ParamEyeROpen` (50%) via `physics_modifiers` (`mobility: 0.95`, `delay: 1.2`, `acceleration: 1.1`) and author a tiny `1–2%` vertical squash/stretch on the iris/eyelash deformer along `ParamEyeBounce`. Every blink gains crisp anime "jelly" rebound.
   ```json
   {
     "id": "PhysicsEyeBounce",
     "name": "Blink Eye Bounce",
     "layer_ids": ["irides_l", "irides_r"],
     "inputs": [
       { "parameter": "ParamEyeLOpen", "weight": 50.0, "type": "x" },
       { "parameter": "ParamEyeROpen", "weight": 50.0, "type": "x" }
     ],
     "outputs": [{ "parameter": "ParamEyeBounce", "vertex": 1, "scale": 1.0 }],
     "segments": [
       { "length": 10.0, "mobility": 0.95, "delay": 1.2, "acceleration": 1.1 }
     ]
   }
   ```
2. **Paired XY (lateral + vertical) & length-graded hair pendulums:**
   - Pair every hair group with **both** a lateral pendulum (`ParamAngleX` 70% + `ParamAngleZ` 30%) **and** a vertical bounce pendulum (`ParamAngleY` 100%).
   - Grade pendulum lengths by hair mass so bangs, side locks, and long back hair oscillate out of phase: short front bangs (`length ~10`, `delay 0.9`, `accel 1.2`), medium side locks (`length 15–18`, `delay 0.8`, `accel 1.0`), and long back hair as a **3-segment chain** (`10 → 10 → 8`, `mobility 0.85 → 0.90 → 0.90`, `accel 1.0 → 1.0 → 0.8`).
   - **Avoiding PSD2Live 3.8.4 `IdTruncated` drops & `.moc3` bloat:** PSD2Live's built-in `hair` modifier and `swing_put` emit >64-byte hex-encoded output parameter IDs that Cubism `.moc3` truncates, leaving dead physics outputs in `.physics3.json`. Disabling `"hair"` in `disable_modifiers` and replacing `swings` with short-ID (`<64` ASCII bytes) `author_axes` + `physics_modifiers` (each with explicit non-empty `layer_ids`, required by PSD2Live's MCP schema) keeps all custom hair, ribbon, eyewear, and eye-bounce outputs 100% matched in `.moc3`. When authoring paired XY axes on dense ArtMeshes, prefer attaching them to lightweight `canvas_warps` deformers rather than directly on the ArtMesh to avoid 4-axis grid bloat in `.moc3`.

## 7. Orthogonal Prop / Pose Toggles, `Blend: "Add"` & Ambient Loops

- Keep facial expressions (`exp_smile`, `exp_cry`, ...) strictly separate from accessory/pose toggles. Strip all `ParamEye*`, `ParamMouth*`, `ParamCheek`, and `ParamBrow*` entries from prop/pose `exp3` files so any prop works with any facial expression.
- Prefer single-parameter `0 → 1` toggles with `"blend": "ADD"` (exported as `"Blend": "Add"` in `.exp3.json`) for facial overlays (blush, tears, sweat, pale shadow) and independent accessories/arms so hotkeys in VTube Studio combine freely without overwriting each other.
- Optionally add a short looping idle curve (e.g., `1.3s`, `0 → 1` loop) for continuous ambient shimmer (eye sparkle, tear glint, floating star ornament).
- Place foreground arms and held props in an `arms_fg` layer group **after** `head/hair` so held items never clip behind side hair strands.
- Tag non-face expression/FX/prop layers as `["object"]` (never `["face", "detail"]`) so `face/fitted` does not double-warp them; bind facial FX (`cheeks_flushed`, `pale`, `sweat`, `cry_tears`) directly to the face contour deformer (`顔デフォーマ`) and dial `expression.cheek` strength down to `~20`.

## 8. Calm On-Model Motion Limits & Auto-Rig Boundaries

Default auto-rig amplitudes often exaggerate anime jawlines and mouse tracking:

- Clamp `ParamAngleX` to `[-25, 25]`, `ParamAngleY` to `[-18, 18]`, `ParamAngleZ` to `[-15, 15]`.
- Scale built-in `Idle`, `Nod`, and `Shake` motion curves (`ParamAngle*` × `0.65`, `ParamBodyAngle*` × `0.70`) **before** applying parameter range clamps.
- Dial down `face/fitted` (`strength: 0.46`, `headShellTurn: 0.34`, `turn: 20`, `look: 42`, `depth: 24`), `mouth.jaw` (`share: 5.0`), and `body/legacy` (`strength: 0.48`) to keep the artist's native V-jaw silhouette clean across extreme turns.
- **Auto-rig vs. hand-sculpted boundary:** PSD2Live auto-rig + `post_rig.json` produces ~30–40 deformers suitable for fast turnaround, interactive WebGL previews, and VTube Studio streaming. Reaching hand-rigged studio density (~150+ warp deformers, multi-tier overskirt/petticoat/necklace depth splits, 3-part anatomical lip rigging, and per-strand hair warps) requires either richer input part separation (artist PSD / SAM2 segmentation) or opening the exported project in Live2D Cubism Editor for manual sculpting.

---

## Appendix: Character Case Study Example

When adapting these patterns to a specific character with multi-part game sprites (for example, a Gothic-dress character wearing a lace headdress with hanging ribbons, shoulder-strap bows, and held sketchbook / plushie props):

- **Unified live mouth source:** Pick a clean open mouth sprite such as `Mouth_Yell_Open` as the single `["mouth", "open"]` mesh and drive Smile / Cry / Angry / Surprised via `ParamMouthForm`, `ParamMouthPuckerWiden`, and `ParamMouthShrug`.
- **Collarbone bow transfer:** Where See-through merged black collarbone bows into `body_back_hair`, extract non-blue dark bow components (`(b - r) < 25` with morphological opening) onto `body_topwear` and smooth-fill the silver-blue hair behind them.
- **2-layer headwear + ribbons:** Split the satin bonnet arch (`headdress_outer_band`) from the scalloped lace rim (`headdress_inner_lace`) and left/right hanging ribbons (`headdress_ribbon_l/r`), keeping the side-ponytail star clip always visible behind the headdress.
- **Independent prop toggles:** Wire `exp_no_headdress` (`ParamHeaddress=0`), `exp_sketchbook` (`ParamArmPose=3`), `exp_sketch_text` (`ParamSketchText=1`, `requires: "sketchbook"`), and `exp_sheep_book` (`ParamArmPose=4`) as orthogonal toggles in both `expressions.json` and `web_ui`.
