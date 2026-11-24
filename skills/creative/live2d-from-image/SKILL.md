---
name: live2d-from-image
description: Turn a single character illustration or multi-part character sprites (such as Unity/KiriKiri extracted parts) into a rigged Live2D Cubism v5 model (.moc3, VTube Studio package, WebGL viewer, GIF, and optional 60fps showcase video) using local See-through part decomposition, native-pixel re-projection, and PSD2Live headless MCP without generative AI inpainting. Use whenever the user asks to turn an image, illustration, portrait, or character sprites into Live2D, .moc3, VTube Studio, PSD2Live, See-through rig, fix Live2D blink/mouth/headdress 3D parallax, upgrade a Live2D WebGL viewer with independent prop toggles, or render a deterministic Live2D showcase video.
---

# Image to Live2D (See-through + PSD2Live Headless MCP)

Convert a character illustration or multi-part sprite set into a clean, repeatable Live2D Cubism v5 model (`.moc3`, VTube Studio, WebGL viewer, GIF) locally without generative image inpainting.

Two cross-platform CLI scripts handle the deterministic parts of the pipeline:

- `scripts/live2d_pipeline.py` — coordinate mapping, native-pixel re-projection, bilateral eye/brow splitting, single-image Case 2B alpha-residual recovery, ordered PSD2Live headless MCP rigging & multi-target export, independent-toggle WebGL viewer patching, and `.moc3` / `.model3.json` validation. Run via `uv` (no global installs).
- `scripts/verify_web.mjs` — local static server + headless Playwright WebGL smoke test checking expressions, independent prop toggles, gaze tracking, optional screenshot capture, and zero console/page errors.

PowerShell:

```powershell
$L2D_PY = Join-Path (git rev-parse --show-toplevel) '.agents/skills/live2d-from-image/scripts/live2d_pipeline.py'
function l2d { uv run --quiet --python 3.12 --with pillow --with numpy --with scipy --with psd-tools python -I $L2D_PY @args }
```

bash / zsh / Git Bash:

```bash
L2D_PY="$(git rev-parse --show-toplevel)/.agents/skills/live2d-from-image/scripts/live2d_pipeline.py"
l2d() { uv run --quiet --python 3.12 --with pillow --with numpy --with scipy --with psd-tools python -I "$L2D_PY" "$@"; }
```

For `run-p2l`, set `PSD2LIVE_DIR` (or pass `--p2l-dir <path>`) to your portable PSD2Live 3.8.4+ folder containing `app/` and `mcp_proxy.py`.

## Choose the Right Mode

Inspect the user's input assets first and pick the matching path:

| Input available                                                   | Mode                       | Pipeline                                                                                                                               |
| ----------------------------------------------------------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Single flat-background or transparent illustration                | **Case 2B (Single-Image)** | Cut out background (`transparent-background` skill) → See-through 1280px → `l2d recover-single` → `l2d run-p2l` → `verify_web.mjs`     |
| Multi-part character sprites (body, head, expression eyes, arms)  | **Case 1B v2 (Hybrid)**    | 2-pass See-through (armless body + 1280px head crop) → character builder script importing `live2d_pipeline` → `l2d run-p2l` → WebGL QA |
| Existing `.psd2live` or `layout.json` needing rig/QA/video polish | **Post-Rig / QA / Video**  | Edit `post_rig.json` / `expressions.json` → `l2d run-p2l` → `verify_web.mjs` → deterministic showcase renderer                         |

Read only the references relevant to the current stage:

- Rig architecture & quality fixes (re-projection, Method A mouth, live gaze on open-eye expressions, blink-immune closed-eye expressions, 2-layer convex headdress 3D Z-parallax via Cubism `physics3`, orthogonal prop toggles): [references/rig-patterns.md](references/rig-patterns.md)
- PSD2Live headless MCP setup, `layout.json` v1 & `post_rig.json` schemas, strict ordering invariants, and common MCP traps: [references/psd2live-mcp.md](references/psd2live-mcp.md)
- Upgraded independent-toggle `export_web` viewer, automated Playwright WebGL QA, and deterministic 60fps 120Hz-substep C2-spring video rendering with BGM muxing: [references/web-qa-and-video.md](references/web-qa-and-video.md)

## Workflow

### Step 1: Prepare Inputs & Decompose with See-through

1. Write all intermediate files and exports into the active task folder (e.g., `tasks/<task>/tmp/`), never into the repository root or source asset folders.
2. **Single-image input (Case 2B):**
   - If the illustration has a solid/flat background, remove it cleanly first (e.g., `transparent-background` skill's `imgalpha.py`) so alpha edges are halo-free.
   - Run local See-through at 1280px to produce a layered `.psd`.
   - Run `recover-single` to re-project sharp native pixels onto visible areas and recover parts See-through commonly drops (dark hats/headwear in the top band, shoes in the bottom band):

     ```bash
     l2d recover-single "<char_rgba.png>" "<seethrough.psd>" -o "<work_dir>" --name "<model_name>"
     ```

3. **Multi-part character input (Case 1B v2 Hybrid):**
   - Run See-through **twice** instead of once on the full body: once on the armless body (for back hair, neck, topwear, legwear, footwear) and once on a tight **head crop at full 1280px** so eyes, brows, nose, face, and front hair receive maximum resolution.
   - Write a small per-character builder script in the task folder that imports `unpad_params`, `map_bbox`, `paste_onto_canvas`, `reproject_visible_pixels`, and `split_eyes_and_brows` from `live2d_pipeline.py` to emit `layout.json`, `expressions.json`, `post_rig.json`, and pose sheets. Follow [references/rig-patterns.md](references/rig-patterns.md) when assembling eyes, mouth, headwear, and prop toggles.

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

This starts PSD2Live MCP locally with a random per-run bearer token, imports the layout, generates the `modern-standard` rig, applies `post_rig.json` in PSD2Live's strict dependency order, renders `poses_full.png` / `poses_face.png` / `poses_sabun*.png`, saves `<name>.psd2live`, exports `moc3`, `vtube-studio`, `web`, and `gif`, upgrades `export_web/index.html` when `web_ui` is configured, and validates `export_moc3` + `export_vtube-studio`.

To validate an existing `.moc3` folder standalone:

```bash
l2d validate-moc3 "<out_dir>/export_moc3" --json
```

### Step 3: Visual & Live WebGL QA Loop

1. Inspect `export_report.json` (`moc3_validation.moc3.header_ok == true`, zero failed targets) and review `poses_full.png`, `poses_face.png`, and `poses_sabun*.png` for:
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
