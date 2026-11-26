# PSD2Live Headless MCP & Schema Reference

## Local Tooling Prerequisites

1. **See-through** ([shitagaki-lab/see-through](https://github.com/shitagaki-lab/see-through), CUDA Python 3.12):
   - Install PyTorch CUDA 12.8 (`torch==2.8.0+cu128`) and `pip install -r requirements.txt` inside the See-through repo; link/copy `common/assets` to `assets`.
   - Model weights download automatically from HuggingFace:
     - LayerDiff 3D: `layerdifforg/seethroughv0.0.2_layerdiff3d`
     - Marigold Depth: `24yearsold/seethroughv0.0.1_marigold`
     - SAM Body Parsing: `24yearsold/l2d_sam_iter2`
   - Run from the See-through repository root:
     ```bash
     # 16 GB+ VRAM (bf16 at 1280px)
     python inference/scripts/inference_psd.py --srcp path/to/char.png --save_to_psd
     # 12 GB VRAM
     python inference/scripts/inference_psd.py --srcp path/to/char.png --save_to_psd --group_offload
     # 8 GB VRAM (NF4 quantized, requires requirements-inference-bnb.txt)
     python inference/scripts/inference_psd_quantized.py --srcp path/to/char.png --save_to_psd
     ```
   - Outputs center-square-padded 1280² layers; invert padding back to native source dimensions with `unpad_params(orig_w, orig_h, psd.width)` and `map_bbox(...)` from `scripts/live2d_pipeline.py` (done automatically by `recover-single`).
   - **macOS / Apple Silicon:** Upstream See-through expects CUDA (`cu128`; `bitsandbytes` only for the NF4 low-VRAM path). An MPS run is possible with a small device patch but slow and memory-heavy, and its quality is unverified against CUDA (see SKILL.md Prerequisites); prefer a CUDA machine or the official HuggingFace Space / ModelScope demo (ask the user before uploading artwork).
2. **PSD2Live 3.8.4+** ([tsunehimatoi/psd2live releases](https://github.com/tsunehimatoi/psd2live/releases)):
   - Point `scripts/live2d_pipeline.py run-p2l` at it via `--p2l-dir <path>` or `PSD2LIVE_DIR`:
     - **Windows portable:** `<dir>/app/`, `<dir>/runtime/bin/java.exe`, `<dir>/mcp_proxy.py`
     - **macOS app bundle:** `/Applications/PSD2Live.app` or `/Applications/PSD2Live.app/Contents` (`Contents/app/`, `Contents/runtime/Contents/Home/bin/java`, `Contents/app/resources/mcp_proxy.py`)
     - **Linux portable:** `<dir>/app/`, `<dir>/runtime/bin/java`, `<dir>/mcp_proxy.py` (or `<dir>/tools/mcp_proxy.py`)
   - **Bearer token security note:** PSD2Live 3.8.4's `HeadlessMcp` CLI only accepts `--token <T>` as a process argument (no env-var flag). `run-p2l` generates a fresh random per-run token, uses an ephemeral localhost port (`--port 0`), verifies readiness with an authenticated JSON-RPC `initialize` probe, and redacts the token (`<REDACTED>`) from `_mcp_stdout.log` and `_mcp_stderr.log` on exit. If PSD2Live exits before answering on an auto-picked port (the port was taken in between), it retries on a new port, up to 3 starts; an explicit `--port` is never changed. Ctrl+C, SIGTERM, and SIGHUP also stop the Java process and redact the logs. SIGKILL cannot be caught: after `kill -9`, stop the leftover PSD2Live Java process yourself and redact or delete `_mcp_*.log`.

## `layout.json` (`psd2live-layout` v1)

Painter order: **back to front** (first entry is drawn furthest back).
Full semantic tag vocabulary: [PSD2Live `PSD_LAYER_SPEC.md#semantic-tags`](https://github.com/tsunehimatoi/psd2live/blob/main/docs/en/spec/PSD_LAYER_SPEC.md#semantic-tags).

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
7. **Phase A `author_axes`** — axes on non-clamped parameters whose targets exist before `canvas_warps` (`ParamEyeShape`, `ParamHdYaw`, `ParamHdPitch`, `ParamEyeBounce`).
8. `keyform_applies` — batched in chunks of **30** changes per `keyform_apply` call (e.g., 2D opacity gates on `eye_close_l/r`).
9. `motion_scale` — scales `Idle` / `Nod` / `Shake` curves and Bezier `in`/`out` handles **before** parameter range clamps, because `motion_put` rejects out-of-range keys once a parameter is clamped.
10. `expressions` (`expression_put`) — applied **before** `canvas_warps`, because `canvas_warp` bakes modifier-owned expression parameters.
11. `parameter_defaults` (`min` / `max` range clamps) — clamps `ParamAngleX/Y/Z` cleanly after motions and expressions are updated.
12. `canvas_warps` — user-owned canvas warp deformers.
13. **Phase B `author_axes`** — axes on clamped parameters **and** any axis targeting a deformer created by `canvas_warps` (so custom parameters like `ParamHdYaw` can safely deform a `canvas_warp` deformer).
14. `physics_modifiers` — `modifier_add` (`type: "physics"`) linking head angles or eye blink to custom short-ID parameters (`ParamAngleX -> ParamHdYaw`, `ParamAngleY -> ParamHdPitch`, `ParamEyeLOpen/ROpen -> ParamEyeBounce`, paired XY hair/ribbon physics). **Requires non-empty `layer_ids`** (PSD2Live rejects empty `targets: []`; entries with no resolved `layer_ids` are warned and skipped).
15. `swings` — `swing_put` pendulum physics (note: in PSD2Live 3.8.4, `swing_put` output IDs exceed 64 bytes and truncate in `.moc3`; prefer short-ID `author_axes` + `physics_modifiers` for `.moc3`/VTS targets).

Target reference prefixes resolved automatically in `post_rig.json`:

- `"layer:<layer_id>"` → resolves via `workspace_inspect` `layerId` to `"mesh:<id>"`.
- `"deformer:<deformer_name>"` → resolves via `workspace_overview` deformer names (note: PSD2Live default deformer names may be Japanese such as `頭部コンテナ`, `顔デフォーマ`, `両目`; inspect `overview.json` after an initial run to confirm exact names for your locale/preset).

## Concrete JSON Schema Examples

### `expressions.json` (`expression_put`)

Use `"blend": "ADD"` for single-parameter facial FX and prop toggles so multiple expressions stack cleanly in VTube Studio (exported as `"Blend": "Add"` in `.exp3.json`):

```json
[
  {
    "id": "exp_smile",
    "name": "Smile",
    "entries": [
      { "parameter": "ParamEyeExpr", "value": 1.0, "blend": "OVERWRITE" },
      { "parameter": "ParamMouthForm", "value": 1.0, "blend": "OVERWRITE" },
      { "parameter": "ParamCheek", "value": 0.4, "blend": "ADD" }
    ]
  },
  {
    "id": "exp_tears",
    "name": "Tears",
    "entries": [{ "parameter": "ParamTears", "value": 1.0, "blend": "ADD" }]
  }
]
```

### `author_axes` (custom eye shape / 3D headwear parallax)

```json
"author_axes": [
  {
    "summary": "Eye shape expression deformations",
    "parameter": { "id": "ParamEyeShape", "name": "Eye Shape", "min": -1.0, "max": 2.0, "default": 0.0 },
    "shapes": [
      {
        "target": "deformer:両目",
        "value": -1.0,
        "operations": [{ "type": "scale", "factors": [1.0, 0.85], "pivot": [0.5, 0.88] }]
      },
      {
        "target": "deformer:両目",
        "value": 2.0,
        "operations": [{ "type": "scale", "factors": [1.04, 1.15], "pivot": [0.5, 0.88] }]
      }
    ]
  },
  {
    "summary": "3D headwear yaw parallax",
    "parameter": { "id": "ParamHdYaw", "name": "Headwear Yaw", "min": -1.0, "max": 1.0, "default": 0.0 },
    "shapes": [
      {
        "target": "layer:headwear_inner",
        "value": -1.0,
        "operations": [{ "type": "translate", "delta": [-0.025, 0.0] }]
      },
      {
        "target": "layer:headwear_outer",
        "value": -1.0,
        "operations": [{ "type": "translate", "delta": [-0.012, 0.0] }]
      }
    ]
  }
]
```

### `keyform_applies` (2D blink-immunity opacity gate)

```json
"keyform_applies": [
  {
    "op": "set",
    "target": "layer:eye_close_l",
    "key": { "ParamEyeLOpen": 0.0, "ParamEyeExpr": 0.0 },
    "channels": { "opacity": 1.0 }
  },
  {
    "op": "set",
    "target": "layer:eye_close_l",
    "key": { "ParamEyeLOpen": 0.0, "ParamEyeExpr": 1.0 },
    "channels": { "opacity": 0.0 }
  },
  {
    "op": "set",
    "target": "layer:eye_close_l",
    "key": { "ParamEyeLOpen": 1.0, "ParamEyeExpr": 0.0 },
    "channels": { "opacity": 0.0 }
  },
  {
    "op": "set",
    "target": "layer:eye_close_l",
    "key": { "ParamEyeLOpen": 1.0, "ParamEyeExpr": 1.0 },
    "channels": { "opacity": 0.0 }
  }
]
```

### `canvas_warps` (user-owned warp deformer)

```json
"canvas_warps": [
  {
    "id": "warp_headwear_3d",
    "name": "Headwear 3D Warp",
    "layer_ids": ["headwear_outer", "headwear_inner"],
    "parent_deformer": "頭部コンテナ",
    "rows": 3,
    "columns": 5
  }
]
```

### `swings` (`swing_put` pendulum accessories)

```json
"swings": [
  {
    "id": "swing_ribbon_r",
    "name": "Right Ribbon Swing",
    "layer_id": "headwear_ribbon_r",
    "fulcrum": "top",
    "preset": "accessory",
    "motions": [
      { "kind": "lateral", "amplitude": 0.10 },
      { "kind": "vertical", "amplitude": 0.03 }
    ]
  }
]
```

## Export Targets, Losses & Validation

`run-p2l` exports four targets in one session:

- `export_moc3/` — Cubism v5 `.moc3`, `.model3.json`, `.physics3.json`, `.cdi3.json`, `expressions/*.exp3.json`, `motions/*.motion3.json`, 4096 texture atlases.
- `export_vtube-studio/` — ready-to-drop VTube Studio folder with `.vtube.json`.
- `export_web/` — standalone WASM/WebGL viewer (`index.html`, `p2l.js`, `p2l_runtime.wasm`, `layout.p2lrt`).
- `export_gif/` — quick 512px 15fps animated preview.

`run-p2l` and `validate_moc3_dir()` enforce:

- `*.moc3` starts with ASCII magic `"MOC3"`, valid Cubism version (`1..6`), and size `>= 64` bytes.
- `*.model3.json` parses cleanly, declares `FileReferences.Moc`, and **every referenced file** (`Moc`, `Textures`, `Physics`, `DisplayInfo`, `Expressions`, `Motions`) exists inside the export directory.
- `Groups` checks whether `EyeBlink` and `LipSync` exist and have non-empty `Ids` (reported in `warnings`).
- `physics3.json` parses cleanly and its output `Destination.Id` values are cross-checked against the `.moc3` parameter ID table (flagging `physics_unmatched_outputs` in `warnings`).
- `export_vtube-studio/` must also include `*.vtube.json`.
- `export_losses` summarizes `handling: "dropped"` and `handling: "approximated"` entries from `exports.*.result.losses`. Any `dropped` loss fails `run-p2l` with exit code `1` unless `--allow-export-losses` is explicitly passed.

> **Known PSD2Live 3.8.4 upstream issue (`IdTruncated`):** PSD2Live 3.8.4's auto-generated internal modifier IDs (hex-encoded `modifier:m1:...`, `modifier:m6:...`, `modifier:m33:...`, and `modifier:swing_...`) exceed Cubism's 64-byte record width. PSD2Live truncates & disambiguates them in `.moc3` (logging `IdTruncated` as `handling: "dropped"`), while `layout.physics3.json` retains the untruncated IDs—disconnecting auto-generated hair chain and `swing_put` physics in Cubism/VTS unless fixed upstream or replaced with short-ID `author_axes` + `physics_modifiers` (disabling `"hair"` and leaving `"swings": []`). `validate_moc3_dir()` surfaces any remaining unmatched IDs in `physics_unmatched_outputs`.
