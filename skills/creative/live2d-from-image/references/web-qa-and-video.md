# WebGL Viewer Upgrade, Automated QA & 60fps Showcase Video

## 1. Independent ON/OFF Toggle Web Viewer (`patch-web`)

PSD2Live's stock `export_web/index.html` exposes a single `<select id="expression">` dropdown, so selecting a prop expression (`exp_sketchbook`) cancels the active facial expression (`exp_cry`).

Include a `"web_ui"` section in `post_rig.json` (or run `live2d_pipeline.py patch-web <index.html> --config web_ui.json`) to upgrade the viewer with independent toggle buttons backed by `P2LPlayer.setParameter()` re-asserted inside `player.update(dt)`:

```json
{
  "title": "Character Live2D Viewer",
  "prop_expression_ids": [
    "exp_no_headdress",
    "exp_sketchbook",
    "exp_sheep_book"
  ],
  "toggles": [
    {
      "id": "no_headdress",
      "label": "Headdress OFF",
      "parameter": "ParamHeaddress",
      "on_value": 0.0,
      "off_value": 1.0
    },
    {
      "id": "sketchbook",
      "label": "Sketchbook",
      "parameter": "ParamArmPose",
      "on_value": 3.0,
      "off_value": 0.0,
      "exclusive_group": "arm"
    },
    {
      "id": "sketch_text",
      "label": "SketchText",
      "parameter": "ParamSketchText",
      "on_value": 1.0,
      "off_value": 0.0,
      "requires": "sketchbook"
    },
    {
      "id": "sheep_book",
      "label": "SheepAndBook",
      "parameter": "ParamArmPose",
      "on_value": 4.0,
      "off_value": 0.0,
      "exclusive_group": "arm"
    }
  ]
}
```

- `prop_expression_ids`: hidden from the facial Expression dropdown so props are controlled solely via toggle buttons.
- `exclusive_group`: turns off sibling toggles sharing the same parameter (e.g., mutually exclusive arm poses).
- `requires`: automatically enables a prerequisite toggle (e.g., enabling `SketchText` turns on `Sketchbook`, and turning off `Sketchbook` turns off `SketchText`).

## 2. Automated Headless WebGL QA (`scripts/verify_web.mjs`)

Run against any `export_web` directory (requires `playwright` in the calling workspace):

```bash
node .agents/skills/live2d-from-image/scripts/verify_web.mjs \
  --dir "<out_dir>/export_web" \
  --expression "exp_cry" \
  --toggle "Headdress OFF" \
  --toggle "SheepAndBook" \
  --channel chrome \
  --screenshot "<out_dir>/verify_web_live.png" \
  --json
```

Exits `0` only when:

- Local static HTTP server loads `p2l_runtime.wasm` and `layout.p2lrt` cleanly.
- Requested expression and toggles activate with expected `aria-pressed="true"` state.
- Pointer gaze movement completes without WebGL/JS runtime errors (`errors.length === 0` and `#error` is empty).

## 3. Deterministic 60fps Showcase / 3:4 Portrait Video Rendering

Real-time screen recording drops frames and produces jittery physics. For cinema-smooth 60fps H.264 showcase videos (e.g., 1080×1440 3:4 portrait or 1920×1080 landscape):

1. **Serve a custom renderer HTML page** alongside `export_web/` (`p2l.js`, `p2l_runtime.wasm`, `layout.p2lrt`) using a local Node HTTP server and open it in Playwright Chromium (`--use-gl=angle` / GPU acceleration when available).
2. **Drive `P2LPlayer` deterministically instead of `requestAnimationFrame`:**
   - Call `player.stop()` right after `P2LPlayer.create(...)`.
   - For each output frame (`dt = 1 / 60`), step internal physics in `SUBSTEPS = 2` (120Hz) so hair and ribbon pendulums stay silky with zero numerical instability.
   - Smooth camera `zoom`, `focusY`, and gaze `lookAt(x, y)` through an analytical **C2 critically-damped 2nd-order spring** (`x' = target + (dx + (v + w*dx)*dt)*exp(-w*dt)`) so velocity and acceleration have zero abrupt kinks.
3. **Real-time live cross-dissolve for discrete prop/arm switches:**
   - Because `ParamArmPose` switches discrete sprite layers (0→1→2→3→4), abrupt single-frame cuts look harsh.
   - During a 0.34s transition window, render the **same live animated frame** twice onto an offscreen canvas—once at `fromPose` and once at `toPose`—and composite `toPose` with `globalAlpha = smootherstep(u)` over `fromPose`. Hair, eyes, breathing, and physics continue moving seamlessly throughout the dissolve.
4. **Stream raw RGBA frames directly into `ffmpeg` (`imageio-ffmpeg` via `uv`, from a trusted folder):**
   - Resolve `ffmpeg` via `uv run --no-project --no-config --no-build --exclude-newer P7D --python 3.12 --with imageio-ffmpeg==0.8.1 python -I -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"`.
   - Pipe `1080x1440` (or `1920x1080`) raw RGBA frames (`-f rawvideo -pix_fmt rgba -s WxH -r 60 -i -`) into `libx264 -crf 17 -preset slow -pix_fmt yuv420p -colorspace bt709 -color_primaries bt709 -color_trc bt709 -movflags +faststart`.
   - Save 6–9 keyframe PNGs at chapter timestamps during rendering and visually inspect them for framing, clipping, and expression accuracy.
5. **Optional lossless BGM muxing:**
   - Extract the desired slice with matching fade-in/fade-out (`afade=t=in:st=0:d=0.7,afade=t=out:st=29.0:d=1.0`, 48kHz stereo AAC 192k), then mux losslessly (`-c:v copy -c:a copy -shortest`) so video quality is untouched.
