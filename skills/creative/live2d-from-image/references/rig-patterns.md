# Proven Live2D Rig Patterns (Zero Generative Inpainting)

These six patterns eliminate the most common failure modes when turning illustrations or game character sprites into production-ready Live2D Cubism v5 models with See-through and PSD2Live.

## 1. Two-Pass See-through + Native-Pixel Re-projection

See-through outputs a 1280×1280 square padded PSD whose diffusion-reconstructed pixels are softer than original artist lineart and occasionally drop dark hats or shoes.

- **Two-pass decomposition (multi-part characters):**
  1. Composite an armless body image and run See-through once to split `back hair`, `neck`, `legwear`, `footwear`, `topwear`, and `neckwear`.
  2. Crop tightly around the head and run See-through a second time at 1280px so `face`, `front hair`, `nose`, `ears`, `eyebrow`, `eyewhite`, `irides`, and `eyelash` keep full pixel detail.
- **Native-pixel re-projection (`reproject_visible_pixels`):**
  - Process layers front-to-back, accumulating an `occluder_mask` of opaque pixels from layers in front.
  - Wherever a layer is visible in the original artwork and not covered by `binary_dilation(occluder_mask, 3)`, blend crisp original artist RGB back in with a Gaussian feather (`sigma=1.5`).
  - Behind occluders, keep See-through's inpainted pixels so head turns and hair sway never expose blank seams.
- **Three high-value cleanup rules:**
  - **Prevent double-nose shadow drift:** Exclude a dilated nose mask (`iterations=12`) from `head_face` re-projection so See-through's clean skin stays behind `head_nose`, while re-projecting crisp native pixels onto `head_nose`.
  - **Move clothing ribbons off back hair:** If See-through merges collarbone/shoulder strap bows into `body_back_hair`, extract non-blue dark bow components onto `body_topwear` and smooth-fill the back hair behind them so bows move with clothing instead of hair.
  - **Preserve native outer contour for accessory-OFF hair:** Never chop a character's back-hair outer contour at an arbitrary Y coordinate when building a bare-head variant. Keep 100% of the original `Body.png` outer silhouette and charcoal ink line, and replace only the interior shadow pixels (`binary_erosion(silhouette, 4)`) with the bare-crown radial colour profile.

## 2. Live Open-Eye Expressions + Blink-Immune Closed-Eye Expressions

Replacing eyes with flat static expression sprites freezes eyeball gaze tracking and blink.

- **Open-eye expressions (Flushed, Pensive, Cry, Angry, Surprised):**
  - Keep `ParamEyeExpr = 0` so the separated live `eyewhite` / `irides` / `eyelash` layers remain active with full gaze and blink.
  - Never lock `ParamEyeLOpen` / `ParamEyeROpen` inside open-eye `exp3` files.
  - Author a custom `ParamEyeShape` axis on the `DeformEyes` deformer (e.g., `-1.0` narrowed angry, `+1.0` softened sad/cry, `+2.0` wide surprised) and combine with rigged eyebrows (`ParamBrowLY/RY`, `Angle`, `Form`).
  - For tears (`Cry`), extract only the tear drops/streams by subtracting the neutral eye sprite and add them as a dedicated `[object]` toggle layer (`ParamCryTears`) bound to the face contour deformer.
- **Closed-eye expressions (Smile, Gloom):**
  - Assign dedicated `ParamEyeExpr` switch IDs (`1`, `2`, ...) to the artist's closed-eye variants (tagged `[object]` and bound to the face contour deformer so `face/fitted` does not double-warp them).
  - Use `split_eyes_and_brows` on the neutral closed-eye sprite for live auto-blink (`eye_close_l/r`, tagged `["eye", "closed", "left"|"right"]`, with `eye.see-through` and `brow.see-through` disabled).
  - **Critical blink gate:** Apply 2D `keyform_apply` opacity keys on `eye_close_l/r` across `(ParamEyeLOpen/ROpen, ParamEyeExpr)` so `eye_close_l/r` has `opacity: 1.0` ONLY at `(Open=0.0, ParamEyeExpr=0.0)` and `opacity: 0.0` at `ParamEyeExpr >= 0.5`. Without this gate, EyeBlink or Blink motions flash neutral closed lids on top of Smile/Gloom.

## 3. Method A: Single Unified Live Rigged Mouth

Swapping static mouth sprites per expression causes squashing at `ParamMouthOpenY=0`, duplicated oval rings from `mouth.outline`, and jarring position jumps during lip-sync.

- Disable the `mouth.outline` modifier in `post_rig.json`.
- Keep **one** live mouth layer (`mouth_open`, tagged `["mouth", "open"]`, using a clean open mouth sprite such as `Mouth_Yell_Open`).
- Drive every expression's resting and talking mouth shape purely through Live2D mouth parameters:
  - `ParamMouthForm` (`+1.0` smile, `-0.85` cry, `-0.95` angry, `-0.35` pensive)
  - `ParamMouthPuckerWiden` (`+0.25` shy/flushed, `-0.45` surprised)
  - `ParamMouthShrug` (`+0.25` angry)
- Result: identical mouth anchor position across all expressions, continuous closed-to-open lip-sync on every expression, and zero double-line artefacts.

## 4. 3D Headwear / Headdress Parallax via 2-Layer Split + Cubism `physics3`

Two PSD2Live pitfalls break standard headwear rigging:

1. Binding headwear to `DeformHairFrontFollow` (`前髪 頭部追従`) stretches the far edge outward and pushes the crown peak opposite to the head turn, reading like a backwards 2D sticker.
2. Calling `author_axis` directly on parent-owned `ParamAngleX` / `ParamAngleY` is silently ignored by PSD2Live (`forms: 1`).

The clean, native Cubism v5 solution:

1. **Split into 2 depth layers along a natural seam** (e.g., black satin band separating an outer bonnet/brim from an inner scalloped lace rim):
   - Give the rear `outer_band` layer a smooth solid-fabric underfill extending ~4–5% inward behind the front `inner_lace` layer so Z-parallax never opens a gap or reveals doubled lace loops.
   - Separate hanging left/right ribbons for independent pendulum physics (`swing_put`).
2. **Rebind all headwear meshes** off `DeformHairFrontFollow` onto the clean head container (`頭部コンテナ` / `DeformHeadContainer`).
3. **Author subtle convex 3D parallax on new dedicated parameters** (`ParamHdYaw` `[-1, 1]` and `ParamHdPitch` `[-1, 1]`):
   - Use a 5×3 `landmarks` grid where the front `inner_lace` shifts ~8–10 px farther toward the turn than the rear `outer_band`, while the far side compresses gently inward and the crown peak stays highest.
   - Sync ribbon and hairclip translations analytically from the outer band's temple anchor delta (`dx_px / child_width`, `dy_px / child_height`).
4. **Drive `ParamHdYaw` / `ParamHdPitch` 1:1 from `ParamAngleX` / `ParamAngleY` via native Cubism `physics3` angle-input modifiers:**

```json
{
  "id": "PhysicsHdYaw",
  "name": "Headwear 3D Yaw",
  "layer_ids": [
    "headdress_outer_band",
    "headdress_inner_lace",
    "headdress_ribbon_r",
    "headdress_ribbon_l"
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

Because it compiles into standard `.physics3.json`, the 3D parallax works identically in `.moc3`, VTube Studio, Cubism Viewer, and WebGL.

## 5. Orthogonal Prop / Pose Toggles & Foreground Draw Order

- Keep facial expressions (`exp_smile`, `exp_cry`, ...) strictly separate from accessory/pose toggles (`exp_no_headdress`, `exp_sketchbook`, `exp_sheep_book`). Strip all `ParamEye*`, `ParamMouth*`, `ParamCheek`, and `ParamBrow*` entries from prop/pose `exp3` files so any prop works with any facial expression.
- Place foreground arms and held props (e.g., plushies, books, raised sleeves) in an `arms_fg` layer group **after** `head/hair` (specifically after side ponytails) so held items never clip behind hair strands.
- Tag non-face expression/FX/prop layers as `["object"]` (never `["face", "detail"]`) so `face/fitted` does not double-warp them; bind facial FX (`cheeks_flushed`, `pale`, `sweat`, `cry_tears`) directly to the face contour deformer (`顔デフォーマ`) and dial `expression.cheek` strength down to `~20`.

## 6. Calm On-Model Motion Limits

Default auto-rig amplitudes often exaggerate anime jawlines and mouse tracking:

- Clamp `ParamAngleX` to `[-25, 25]`, `ParamAngleY` to `[-18, 18]`, `ParamAngleZ` to `[-15, 15]`.
- Scale built-in `Idle`, `Nod`, and `Shake` motion curves (`ParamAngle*` × `0.65`, `ParamBodyAngle*` × `0.70`) **before** applying parameter range clamps.
- Dial down `face/fitted` (`strength: 0.46`, `headShellTurn: 0.34`, `turn: 20`, `look: 42`, `depth: 24`), `mouth.jaw` (`share: 5.0`), and `body/legacy` (`strength: 0.48`) to keep the artist's native V-jaw silhouette clean across extreme turns.
