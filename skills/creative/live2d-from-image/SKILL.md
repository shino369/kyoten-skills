---
name: live2d-from-image
description: Turn a single character illustration or multi-part character sprites (such as Unity/KiriKiri extracted parts) into a rigged Live2D Cubism v5 model (.moc3, VTube Studio package, WebGL viewer, GIF, and optional 60fps showcase video) using local See-through part decomposition, native-pixel re-projection, and PSD2Live headless MCP without external generative image editing. Use whenever the user asks to turn an image, illustration, portrait, or character sprites into Live2D, .moc3, VTube Studio, PSD2Live, See-through rig, fix Live2D blink/mouth/headdress 3D parallax, upgrade a Live2D WebGL viewer with independent prop toggles, or render a deterministic Live2D showcase video.
---

# Image to Live2D (See-through + PSD2Live Headless MCP)

Convert a character illustration or multi-part sprite set into a clean, repeatable Live2D Cubism v5 model (`.moc3`, VTube Studio, WebGL viewer, GIF) locally without external generative image editing (relying on See-through's built-in hidden-area completion and native-pixel re-projection).

Two cross-platform CLI scripts handle the deterministic parts of the pipeline:

- `scripts/live2d_pipeline.py` — coordinate mapping, native-pixel re-projection, bilateral eye/brow splitting (front and three-quarter views), single-image Case 2B native-resolution alpha-residual recovery, cross-platform PSD2Live headless MCP rigging & multi-target export, independent-toggle WebGL viewer patching, export-loss auditing, and `.moc3` / `.model3.json` / `.physics3.json` validation. Run via `uv` (no global installs).
- `scripts/verify_web.mjs` — local static server + headless Playwright WebGL smoke test checking expressions, independent prop toggles, gaze tracking, optional screenshot capture, and zero console/page errors.

## Prerequisites

1. **`uv` (Python 3.12 runtime)** — runs `scripts/live2d_pipeline.py` with isolated ephemeral packages (`pillow`, `numpy`, `scipy`, `psd-tools`).
2. **See-through** ([shitagaki-lab/see-through](https://github.com/shitagaki-lab/see-through)) — local layer decomposition into up to 23 semantic PSD layers:
   - Requires a CUDA GPU (~12–16 GB VRAM in bf16; `--group_offload` for 12 GB; `inference_psd_quantized.py` with `bitsandbytes` for 8 GB).
   - Auto-downloads HuggingFace weights on first run: `layerdifforg/seethroughv0.0.2_layerdiff3d`, `24yearsold/seethroughv0.0.1_marigold`, and `24yearsold/l2d_sam_iter2`.
   - Main CLI command (run from the See-through repo root):
     `python inference/scripts/inference_psd.py --srcp <input.png> --save_to_psd`
   - **macOS / Apple Silicon note:** Upstream See-through assumes CUDA; MPS is unverified and unreliable. On macOS, run See-through on a CUDA machine or use the official HuggingFace Space / ModelScope demo (ask the user before uploading artwork).
3. **PSD2Live 3.8.4+** ([tsunehimatoi/psd2live releases](https://github.com/tsunehimatoi/psd2live/releases)) — headless MCP rigging & export:
   - Pass `--p2l-dir <path>` or set `PSD2LIVE_DIR`:
     - **Windows portable:** `<dir>` containing `app/`, `runtime/bin/java.exe`, and `mcp_proxy.py`
     - **macOS app bundle:** `/Applications/PSD2Live.app` (or `PSD2Live.app/Contents`, auto-probes `Contents/app/resources/mcp_proxy.py` and `Contents/runtime/Contents/Home/bin/java`)
     - **Linux portable:** `<dir>` containing `app/`, `runtime/bin/java`, and `mcp_proxy.py` (or `tools/mcp_proxy.py`)
4. **Node.js 24+ & Playwright** (for `scripts/verify_web.mjs`):
   - Install once in the calling workspace: `pnpm add -D playwright && pnpm exec playwright install chromium` (or pass `--channel chrome` to reuse an installed Chrome binary).

## Shell Helpers

PowerShell:

```powershell
$L2D_DIR = Join-Path (git rev-parse --show-toplevel) '.agents/skills/live2d-from-image/scripts'
$L2D_PY = Join-Path $L2D_DIR 'live2d_pipeline.py'
function l2d { uv run --quiet --python 3.12 --with pillow --with numpy --with scipy --with psd-tools python -I $L2D_PY @args }
```

bash / zsh / Git Bash:

```bash
L2D_DIR="$(git rev-parse --show-toplevel)/.agents/skills/live2d-from-image/scripts"
L2D_PY="$L2D_DIR/live2d_pipeline.py"
l2d() { uv run --quiet --python 3.12 --with pillow --with numpy --with scipy --with psd-tools python -I "$L2D_PY" "$@"; }
```

## Choose the Right Mode

Inspect the user's input assets first and pick the matching path:

| Input available                                                   | Mode                       | Pipeline                                                                                                                               |
| ----------------------------------------------------------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Single flat-background or transparent illustration                | **Case 2B (Single-Image)** | Cut out background (`transparent-background` skill) → See-through 1280px → `l2d recover-single` → `l2d run-p2l` → `verify_web.mjs`     |
| Multi-part character sprites (body, head, expression eyes, arms)  | **Case 1B v2 (Hybrid)**    | 2-pass See-through (armless body + 1280px head crop) → character builder script importing `live2d_pipeline` → `l2d run-p2l` → WebGL QA |
| Existing `.psd2live` or `layout.json` needing rig/QA/video polish | **Post-Rig / QA / Video**  | Edit `post_rig.json` / `expressions.json` → `l2d run-p2l` → `verify_web.mjs` → deterministic showcase renderer                         |

Read only the references relevant to the current stage:

- Rig architecture, Case 1B sprite placement/alignment, eye tagging, 3-layer headwear & clipped cast shadows, blink-driven eye physics, XY multi-chain hair physics, and character case-study appendix: [references/rig-patterns.md](references/rig-patterns.md)
- PSD2Live headless MCP setup (Windows/macOS/Linux), `layout.json` v1 & `post_rig.json` schemas with complete JSON examples, tag vocabulary link, strict ordering invariants, and export-loss / token-redaction rules: [references/psd2live-mcp.md](references/psd2live-mcp.md)
- Upgraded independent-toggle `export_web` viewer, automated Playwright WebGL QA, and deterministic 60fps 120Hz-substep C2-spring video rendering with BGM muxing: [references/web-qa-and-video.md](references/web-qa-and-video.md)

## Workflow

### Step 1: Prepare Inputs & Decompose with See-through

1. Write all intermediate files and exports into the active task folder (e.g., `tasks/<task>/tmp/`), never into the repository root or source asset folders.
2. **Single-image input (Case 2B):**
   - If the illustration has a solid/flat background, remove it cleanly first (e.g., `transparent-background` skill's `imgalpha.py`) so alpha edges are halo-free.
   - Run local See-through at 1280px to produce a layered `.psd`.
   - Run `recover-single` to map See-through's 1280² layers back to the source image's **full native resolution**, re-project sharp native pixels onto visible areas, and recover parts See-through commonly drops (dark hats/headwear in the top band, shoes in the bottom band):

     ```bash
     l2d recover-single "<char_rgba.png>" "<seethrough.psd>" -o "<work_dir>" --name "<model_name>"
     ```

3. **Multi-part character input (Case 1B v2 Hybrid):**
   - Run See-through **twice** instead of once on the full body: once on the armless body (for back hair, neck, topwear, legwear, footwear) and once on a tight **head crop at full 1280px** so eyes, brows, nose, face, and front hair receive maximum resolution.
   - Because `python -I` (isolated mode) ignores `PYTHONPATH` and the current directory, add the skill's `scripts/` directory explicitly via `sys.path.insert(0, ...)` at the top of your per-character builder script:

     ```python
     import sys
     from pathlib import Path

     SCRIPTS_DIR = Path("<repo_root>/.agents/skills/live2d-from-image/scripts").resolve()
     sys.path.insert(0, str(SCRIPTS_DIR))

     from live2d_pipeline import (
         map_bbox,
         paste_onto_canvas,
         reproject_visible_pixels,
         split_eyes_and_brows,
         unpad_params,
     )
     ```

   - Follow [references/rig-patterns.md](references/rig-patterns.md) for sprite scale/placement calibration, variant alignment, whole-eye tagging pitfalls, Method A mouth, headwear parallax, and prop toggles, and refer to [PSD2Live semantic tags](https://github.com/tsunehimatoi/psd2live/blob/main/docs/en/spec/PSD_LAYER_SPEC.md#semantic-tags) when assigning `"tags"` in `layout.json`.

### Step 2: Rig, Post-Process & Export via Headless PSD2Live MCP

Run the deterministic headless pipeline from `layout.json` (or `--psd`):

```bash
l2d run-p2l \
  --layout "<work_dir>/layout.json" \
  --out "<out_dir>" \
  --name "<model_name>" \
  --p2l-dir "<path/to/psd2live>" \
  --post-rig "<work_dir>/post_rig.json" \
  --expressions "<work_dir>/expressions.json" \
  --extra-poses "<work_dir>/extra_poses.json" \
  --sabun-poses "<work_dir>/sabun_poses.json"
```

This picks a free ephemeral port by default (`--port 0`), starts PSD2Live MCP locally with a random per-run bearer token (authenticated JSON-RPC readiness probe + automatic token redaction from `_mcp_*.log`), imports the layout, generates the `modern-standard` rig, applies `post_rig.json` in PSD2Live's strict dependency order, appends `--extra-poses` onto the 6 default face poses (chunking at 9 per sheet), renders `poses_full.png` / `poses_face*.png` / `poses_sabun*.png`, saves `<name>.psd2live`, exports `moc3`, `vtube-studio`, `web`, and `gif`, upgrades `export_web/index.html` when `web_ui` is configured, summarizes compiler `losses`, and strictly validates both `export_moc3` and `export_vtube-studio`.

> **Note on export losses:** `run-p2l` exits non-zero if any export target fails, if `.moc3` / `.model3.json` / `.vtube.json` validation fails, or if PSD2Live reports `handling: "dropped"` compiler losses (such as PSD2Live 3.8.4's known 64-byte `IdTruncated` modifier ID warnings). Inspect `export_report.json` (`export_losses` and `physics_unmatched_outputs`); pass `--allow-export-losses` only when you have reviewed and explicitly accept known upstream losses.

To validate an existing `.moc3` folder standalone:

```bash
l2d validate-moc3 "<out_dir>/export_moc3" --json
```

### Step 3: Visual & Live WebGL QA Loop

1. Inspect `export_report.json` (`moc3_validation.ok == true`, `vts_validation.ok == true`, zero unexpected `export_losses.total_dropped`, check `warnings` such as `physics_unmatched_outputs`) and review `poses_full.png`, `poses_face*.png`, and `poses_sabun*.png` for:
   - Eye blink alignment & closed-eye immunity on smile/gloom expressions
   - Live eye gaze on open-eye expressions
   - Unified mouth shape at rest (`MouthOpenY=0`) and open (`MouthOpenY=1`) with zero double lines
   - Headwear/headdress 3D perspective wrap without backwards stretch or floating parallax
   - Clean silhouettes when accessories are toggled OFF and correct foreground draw order for props
2. Run headless WebGL verification against `export_web`:

   ```bash
   node "<path/to/verify_web.mjs>" \
     --dir "<out_dir>/export_web" \
     --expression "exp_cry" \
     --toggle "Headdress OFF" \
     --screenshot "<out_dir>/verify_web_live.png" \
     --json
   ```

3. Iterate on `post_rig.json` or the part builder script until both pose sheets and live WebGL pass with zero console errors.

### Step 4: Optional Showcase Video & Packaging

If the user wants a showcase or 3:4 portrait demo video, follow [references/web-qa-and-video.md](references/web-qa-and-video.md) to drive `P2LPlayer` deterministically in Playwright at 60fps (with 120Hz internal physics sub-stepping and C2 critically-damped springs) and encode via `imageio-ffmpeg`.

Before delivering or publishing third-party `.moc3` exports, remind the user to verify artwork rights and Live2D Cubism / VTube Studio licence terms for their intended use.

## Tests

When developing inside the skill repository, run both suites at 100% branch coverage:

```bash
pnpm test:coverage
uv run --quiet --python 3.12 --with pytest --with pytest-cov --with pillow --with numpy --with scipy python -I -B -m pytest -q -p no:cacheprovider skills/creative/live2d-from-image/scripts --cov=live2d_pipeline --cov-branch --cov-fail-under=100
```
