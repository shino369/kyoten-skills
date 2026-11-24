# PSD2Live Headless MCP & Schema Reference

## Local Tooling Prerequisites

1. **See-through** (local CUDA Python environment):
   - Runs part decomposition at `1280` resolution producing layered `.psd` files.
   - Outputs center-square-padded layers; invert padding with `unpad_params(orig_w, orig_h, psd.width)` and `map_bbox(...)` from `scripts/live2d_pipeline.py`.
2. **PSD2Live Portable (3.8.4+)**:
   - Directory containing `app/`, `mcp_proxy.py`, and bundled `runtime/bin/java[.exe]` (or system `java` on PATH).
   - Point `scripts/live2d_pipeline.py run-p2l` at it via `--p2l-dir <path>` or the `PSD2LIVE_DIR` environment variable.

## `layout.json` (`psd2live-layout` v1)

Painter order: **back to front** (first entry is drawn furthest back).

```json
{
  "format": "psd2live-layout",
  "version": 1,
  "name": "character_v1",
  "canvas": { "width": 2416, "height": 3456 },
  "groups": [
    { "path": "body/hair", "visible": true, "opacity": 1 },
    { "path": "head/face", "visible": true, "opacity": 1 },
    { "path": "arms_fg", "visible": true, "opacity": 1 }
  ],
  "layers": [
    {
      "id": "head_face",
      "name": "face",
      "image": { "path": "parts/head_face.png", "width": 700, "height": 740 },
      "rect": { "left": 860.0, "top": 250.0, "width": 700.0, "height": 740.0 },
      "group": "head/face",
      "tags": ["face"],
      "visible": true,
      "opacity": 1.0
    },
    {
      "id": "prop_book",
      "name": "Book",
      "image": { "path": "parts/book.png", "width": 600, "height": 500 },
      "rect": { "left": 900.0, "top": 1100.0, "width": 600.0, "height": 500.0 },
      "group": "arms_fg",
      "tags": ["object"],
      "variant": {
        "type": "switch",
        "parameter": "ParamArmPose",
        "switch_id": 3
      },
      "visible": true,
      "opacity": 1.0
    }
  ]
}
```

Variant modes:

- `{"type": "switch", "parameter": "ParamArmPose", "switch_id": 0}` — mutually exclusive integer states.
- `{"type": "toggle", "parameter": "ParamCheek"}` — independent 0/1 visibility switch.

## `post_rig.json` Ordering Invariants

`apply_post_rig()` in `scripts/live2d_pipeline.py` enforces this exact order because violating it triggers silent no-ops or validation errors inside PSD2Live:

1. `settings_update` — project-wide rig settings.
2. `disable_modifiers` & `modifier_updates` — disable `eye.see-through`, `brow.see-through`, `mouth.outline` and tune `face/fitted`, `body/legacy`, `hair`, `mouth.jaw`, `expression.cheek`.
3. `workspace_inspect(scope="objects")` — **must paginate at `limit=64` with `offset`**. Models with >64 meshes drop layers silently if queried without pagination.
4. `parameter_defaults` (name & default only) — set defaults before axes without clamping min/max yet.
5. `bind_to_deformer` — `object_edit_appearance` (`action: "bind"`, `space: "canvas"`). Avoid binding custom meshes to `DeformEyeShapeL/R` directly (can invalidate `eye/fitted`); bind facial overlays to the face contour deformer (`顔デフォーマ`) and headwear to the head container (`頭部コンテナ`).
6. `create_warps` — `rig_create_warp`.
7. **Phase A `author_axes`** — axes on non-clamped parameters (`ParamEyeShape`, `ParamHdYaw`, `ParamHdPitch`).
8. `keyform_applies` — batched in chunks of **30** changes per `keyform_apply` call (e.g., 2D opacity gates on `eye_close_l/r`).
9. `motion_scale` — scales `Idle` / `Nod` / `Shake` curves and Bezier `in`/`out` handles **before** parameter range clamps, because `motion_put` rejects out-of-range keys once a parameter is clamped.
10. `expressions` (`expression_put`) — applied **before** `canvas_warps`, because `canvas_warp` bakes modifier-owned expression parameters.
11. `parameter_defaults` (`min` / `max` range clamps) — clamps `ParamAngleX/Y/Z` cleanly after motions and expressions are updated.
12. `canvas_warps` — user-owned canvas warp deformers.
13. **Phase B `author_axes`** — axes on clamped parameters (if any).
14. `physics_modifiers` — `modifier_add` (`type: "physics"`) linking head angles to custom 3D parallax parameters (`ParamAngleX -> ParamHdYaw`, `ParamAngleY -> ParamHdPitch`).
15. `swings` — `swing_put` pendulum physics for ribbons, hair ornaments, and accessories.

Target reference prefixes resolved automatically in `post_rig.json`:

- `"layer:<layer_id>"` → resolves via `workspace_inspect` `layerId` to `"mesh:<id>"`.
- `"deformer:<deformer_name>"` → resolves via `workspace_overview` deformer names (note: PSD2Live default deformer names may be Japanese such as `頭部コンテナ`, `顔デフォーマ`, `両目`; inspect `overview.json` after an initial run to confirm exact names for your locale/preset).

## Export Targets & Validation

`run-p2l` exports four targets in one session:

- `export_moc3/` — Cubism v5 `.moc3`, `.model3.json`, `.physics3.json`, `.cdi3.json`, `expressions/*.exp3.json`, `motions/*.motion3.json`, 4096 texture atlases.
- `export_vtube-studio/` — ready-to-drop VTube Studio folder with `.vtube.json`.
- `export_web/` — standalone WASM/WebGL viewer (`index.html`, `p2l.js`, `p2l_runtime.wasm`, `layout.p2lrt`).
- `export_gif/` — quick 512px 15fps animated preview.

`validate_moc3_dir()` checks:

- `*.moc3` starts with ASCII magic `"MOC3"` and valid Cubism version (`1..6`).
- `*.model3.json` lists textures, physics, expressions, motions, and `EyeBlink` / `LipSync` groups.
