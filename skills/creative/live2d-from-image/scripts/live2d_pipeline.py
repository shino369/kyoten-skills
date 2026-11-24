#!/usr/bin/env python3
"""live2d_pipeline: reusable image-to-Live2D (.moc3 / VTS / WebGL / GIF) CLI.

Subcommands (run with uv, nothing installed globally):
  recover-single  Build a Case 2B psd2live-layout manifest from a transparent
                  illustration + See-through PSD (with native-pixel
                  re-projection and alpha-residual headwear/footwear recovery).
  split-eyes      Split a bilateral eye/brow PNG into right/left eye & brow PNGs.
  run-p2l         Run PSD2Live headless MCP: import layout/PSD -> rig ->
                  ordered post_rig recipe -> expressions -> pose sheets ->
                  export moc3/vtube-studio/web/gif -> validate MOC3.
  patch-web       Upgrade export_web/index.html with independent ON/OFF
                  prop/pose toggles combinable with any facial expression.
  validate-moc3   Inspect *.moc3 header and *.model3.json references.
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

DEFAULT_SEETHROUGH_TAG_MAP: dict[str, list[str]] = {
    "back hair": ["hair", "back"],
    "front hair": ["hair", "front"],
    "headwear": ["headwear"],
    "face": ["face"],
    "ears-r": ["ear", "right"],
    "ears-l": ["ear", "left"],
    "nose": ["nose"],
    "mouth": ["mouth", "open"],
    "eyebrow-l": ["brow", "left"],
    "eyebrow-r": ["brow", "right"],
    "eyewhite-l": ["eye", "white", "left"],
    "eyewhite-r": ["eye", "white", "right"],
    "irides-l": ["eye", "iris", "left"],
    "irides-r": ["eye", "iris", "right"],
    "eyelash-l": ["eye", "lash", "left"],
    "eyelash-r": ["eye", "lash", "right"],
    "neck": ["neck"],
    "topwear": ["top"],
    "bottomwear": ["bottom"],
    "legwear": ["leg"],
    "footwear": ["foot"],
    "handwear-l": ["arm", "left"],
    "handwear-r": ["arm", "right"],
    "objects": ["object"],
}

DEFAULT_EXPORT_TARGETS: tuple[tuple[str, dict[str, str]], ...] = (
    ("moc3", {}),
    ("vtube-studio", {}),
    ("web", {}),
    ("gif", {"size": "512", "fps": "15"}),
)


# ---------- Coordinate mapping & native-pixel re-projection ----------


def unpad_params(orig_w: int, orig_h: int, res: int = 1280) -> tuple[float, float, float]:
    """Return (scale, pad_x, pad_y) matching See-through's center-square pad/resize."""
    if orig_w <= 0 or orig_h <= 0 or res <= 0:
        raise ValueError(f"Dimensions must be positive, got orig=({orig_w}, {orig_h}), res={res}")
    scale = res / max(orig_w, orig_h)
    nw = int(round(orig_w * scale))
    nh = int(round(orig_h * scale))
    return scale, float((res - nw) // 2), float((res - nh) // 2)


def map_bbox(
    bbox: tuple[int, int, int, int],
    scale: float,
    pad_x: float,
    pad_y: float,
    off_x: float = 0.0,
    off_y: float = 0.0,
) -> dict[str, float]:
    """Map a See-through PSD layer bbox (x0, y0, x1, y1) onto native canvas coordinates."""
    x0, y0, x1, y1 = bbox
    return {
        "left": round(off_x + (x0 - pad_x) / scale, 2),
        "top": round(off_y + (y0 - pad_y) / scale, 2),
        "width": round((x1 - x0) / scale, 2),
        "height": round((y1 - y0) / scale, 2),
    }


def paste_onto_canvas(
    im: Image.Image,
    rect: dict[str, float],
    cw: int,
    ch: int,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Resize layer to rect and return clipped native-res RGBA array + (dx0, dy0, dx1, dy1)."""
    lx, ly = int(round(rect["left"])), int(round(rect["top"]))
    tw, th = max(1, int(round(rect["width"]))), max(1, int(round(rect["height"])))
    resized = im.convert("RGBA").resize((tw, th), Image.Resampling.LANCZOS)
    arr = np.array(resized)
    sx0, sy0 = max(0, -lx), max(0, -ly)
    dx0, dy0 = max(0, lx), max(0, ly)
    dx1, dy1 = min(cw, lx + tw), min(ch, ly + th)
    if dx1 <= dx0 or dy1 <= dy0:
        raise ValueError(f"Layer rect {rect} lies completely outside canvas {cw}x{ch}")
    crop = arr[sy0 : sy0 + (dy1 - dy0), sx0 : sx0 + (dx1 - dx0)].copy()
    return crop, (dx0, dy0, dx1, dy1)


def reproject_visible_pixels(
    layer_arr: np.ndarray,
    box: tuple[int, int, int, int],
    orig_canvas: np.ndarray,
    occluder_mask: np.ndarray,
    *,
    dilate_iters: int = 3,
    feather_sigma: float = 1.5,
    alpha_threshold: int = 20,
) -> np.ndarray:
    """Replace blurry See-through RGB with sharp original pixels wherever visible.

    Pixels behind occluding front layers retain See-through's inpainted RGB with a
    smooth Gaussian feather transition.
    """
    x0, y0, x1, y1 = box
    out = layer_arr.copy()
    orig_patch = orig_canvas[y0:y1, x0:x1]
    occ_patch = occluder_mask[y0:y1, x0:x1]

    st_alpha = out[:, :, 3] > alpha_threshold
    orig_alpha = orig_patch[:, :, 3] > alpha_threshold
    occ_dil = ndimage.binary_dilation(occ_patch, iterations=dilate_iters)
    vis = st_alpha & orig_alpha & (~occ_dil)

    w = ndimage.gaussian_filter(vis.astype(np.float32), sigma=feather_sigma)
    w = np.clip(w, 0.0, 1.0)[..., None]

    out[:, :, :3] = np.round(
        orig_patch[:, :, :3].astype(np.float32) * w
        + out[:, :, :3].astype(np.float32) * (1.0 - w)
    ).astype(np.uint8)
    outer_vis = (~occ_dil) & (orig_patch[:, :, 3] > 0) & st_alpha
    out[:, :, 3] = np.where(
        outer_vis,
        np.maximum(out[:, :, 3], orig_patch[:, :, 3]),
        out[:, :, 3],
    )
    return out


def split_eyes_and_brows(
    src_path: Path,
    out_parts: Path,
    prefix: str,
    rect: dict[str, float],
    *,
    alpha_threshold: int = 8,
    min_px: int = 10,
    brow_max_y_ratio: float = 0.36,
    brow_max_h_ratio: float = 0.30,
) -> dict[str, tuple[Path, tuple[int, int], dict[str, float]]]:
    """Split a bilateral eye PNG into character right/left eyes and right/left brows.

    Character's right = viewer's left (x < mid_x).
    Character's left  = viewer's right (x >= mid_x).
    """
    out_parts.mkdir(parents=True, exist_ok=True)
    im = Image.open(src_path).convert("RGBA")
    arr = np.array(im)
    h, w = arr.shape[:2]
    mid_x = w // 2
    alpha = arr[:, :, 3] > alpha_threshold
    labeled, n = ndimage.label(alpha)

    buckets: dict[str, np.ndarray] = {
        "eye_r": np.zeros((h, w), dtype=bool),
        "eye_l": np.zeros((h, w), dtype=bool),
        "brow_r": np.zeros((h, w), dtype=bool),
        "brow_l": np.zeros((h, w), dtype=bool),
    }

    for i in range(1, n + 1):
        mask = labeled == i
        ys, xs = np.where(mask)
        if len(xs) < min_px:
            continue
        cx = float(xs.mean())
        y_max = int(ys.max())
        y_min = int(ys.min())
        side = "r" if cx < mid_x else "l"
        if y_max < int(h * brow_max_y_ratio) and (y_max - y_min) < int(h * brow_max_h_ratio):
            buckets[f"brow_{side}"] |= mask
        else:
            buckets[f"eye_{side}"] |= mask

    results: dict[str, tuple[Path, tuple[int, int], dict[str, float]]] = {}
    sx = rect["width"] / w
    sy = rect["height"] / h
    for key, mask in buckets.items():
        if not mask.any():
            continue
        ys, xs = np.where(mask)
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        sub = arr[y0:y1, x0:x1].copy()
        sub[~mask[y0:y1, x0:x1]] = 0
        out_path = out_parts / f"{prefix}_{key}.png"
        Image.fromarray(sub).save(out_path)
        sub_rect = {
            "left": round(rect["left"] + x0 * sx, 2),
            "top": round(rect["top"] + y0 * sy, 2),
            "width": round((x1 - x0) * sx, 2),
            "height": round((y1 - y0) * sy, 2),
        }
        results[key] = (out_path, (x1 - x0, y1 - y0), sub_rect)
    return results


# ---------- Case 2B: Single-image alpha-residual recovery ----------


def sanitize_layer_stem(name: str, fallback: str = "layer") -> str:
    """Sanitize a PSD layer name into a safe single-segment filename stem."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return cleaned or fallback


def _default_psd_loader(psd_path: Path) -> Any:
    psd_tools = importlib.import_module("psd_tools")
    return psd_tools.PSDImage.open(psd_path)


def build_single_image_layout(
    src_rgba: Path,
    seethrough_psd: Path,
    out_dir: Path,
    *,
    name: str = "model",
    top_zone_ratio: float = 0.22,
    bottom_zone_ratio: float = 0.22,
    min_top_px: int = 150,
    min_bottom_px: int = 100,
    reproject: bool = True,
    tag_map: Mapping[str, list[str]] = DEFAULT_SEETHROUGH_TAG_MAP,
    psd_loader: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    """Build a psd2live-layout manifest from a transparent PNG + See-through PSD."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_parts = out_dir / "parts"
    out_parts.mkdir(parents=True, exist_ok=True)

    loader = psd_loader or _default_psd_loader
    psd = loader(seethrough_psd)
    res = int(psd.width)

    orig = Image.open(src_rgba).convert("RGBA")
    ow, oh = orig.size
    scale, pad_x_f, pad_y_f = unpad_params(ow, oh, res)
    nw, nh = int(round(ow * scale)), int(round(oh * scale))
    pad_x, pad_y = int(pad_x_f), int(pad_y_f)
    orig_sq = Image.new("RGBA", (res, res), (0, 0, 0, 0))
    orig_sq.alpha_composite(orig.resize((nw, nh), Image.Resampling.LANCZOS), (pad_x, pad_y))
    orig_arr = np.array(orig_sq)

    raw_layers: list[tuple[int, str, tuple[int, int, int, int], np.ndarray]] = []
    cov = np.zeros((res, res), dtype=np.uint8)
    for idx, layer in enumerate(psd):
        im = layer.composite()
        if im is None:
            continue
        x0, y0, x1, y1 = layer.bbox
        sx0, sy0 = max(0, -x0), max(0, -y0)
        dx0, dy0 = max(0, x0), max(0, y0)
        dx1, dy1 = min(res, x1), min(res, y1)
        if dx1 <= dx0 or dy1 <= dy0:
            continue
        arr_full = np.array(im.convert("RGBA"))
        arr = arr_full[sy0 : sy0 + (dy1 - dy0), sx0 : sx0 + (dx1 - dx0)].copy()
        cov[dy0:dy1, dx0:dx1] = np.maximum(cov[dy0:dy1, dx0:dx1], arr[:, :, 3])
        raw_layers.append((idx, layer.name, (dx0, dy0, dx1, dy1), arr))

    if reproject:
        occ = np.zeros((res, res), dtype=bool)
        reprojected: list[tuple[int, str, tuple[int, int, int, int], np.ndarray]] = []
        for idx, lname, box, arr in reversed(raw_layers):
            out_arr = reproject_visible_pixels(arr, box, orig_arr, occ)
            x0, y0, x1, y1 = box
            occ[y0:y1, x0:x1] |= out_arr[:, :, 3] > 120
            reprojected.append((idx, lname, box, out_arr))
        reprojected.reverse()
        raw_layers = reprojected

    layers: list[dict[str, Any]] = []
    for idx, lname, (x0, y0, x1, y1), arr in raw_layers:
        safe = f"{idx:02d}_{sanitize_layer_stem(lname)}"
        p = out_parts / f"{safe}.png"
        Image.fromarray(arr).save(p)
        item: dict[str, Any] = {
            "id": f"st_{safe}",
            "name": lname,
            "image": {"path": f"parts/{p.name}", "width": arr.shape[1], "height": arr.shape[0]},
            "rect": {"left": float(x0), "top": float(y0), "width": float(x1 - x0), "height": float(y1 - y0)},
            "visible": True,
            "opacity": 1.0,
        }
        if lname in tag_map:
            item["tags"] = list(tag_map[lname])
        layers.append(item)

    cov_bool = ndimage.binary_dilation(cov > 80, iterations=2)
    orig_bool = orig_arr[:, :, 3] > 20
    missing = orig_bool & (~cov_bool)

    def extract_zone(
        mask_zone: np.ndarray, out_name: str, min_px: int
    ) -> tuple[Path, tuple[int, int], dict[str, float]] | None:
        m = missing & mask_zone
        labeled, n = ndimage.label(m)
        keep = np.zeros_like(m)
        for i in range(1, n + 1):
            c = labeled == i
            if int(c.sum()) >= min_px:
                keep |= c
        if not keep.any():
            return None
        keep = ndimage.binary_dilation(keep, iterations=3) & orig_bool
        ys, xs = np.where(keep)
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        crop = orig_arr[y0:y1, x0:x1].copy()
        crop[~keep[y0:y1, x0:x1]] = 0
        p = out_parts / out_name
        Image.fromarray(crop).save(p)
        rect = {"left": float(x0), "top": float(y0), "width": float(x1 - x0), "height": float(y1 - y0)}
        return p, (x1 - x0, y1 - y0), rect

    top_zone = np.zeros((res, res), dtype=bool)
    top_zone[pad_y : pad_y + int(nh * top_zone_ratio), :] = True
    bot_zone = np.zeros((res, res), dtype=bool)
    bot_zone[pad_y + int(nh * (1.0 - bottom_zone_ratio)) : pad_y + nh, :] = True

    shoes_rec = extract_zone(bot_zone, "shoes_recovered.png", min_px=min_bottom_px)
    hat_rec = extract_zone(top_zone, "hat_recovered.png", min_px=min_top_px)

    if shoes_rec is not None:
        sp, ssz, srect = shoes_rec
        layers.insert(
            min(1, len(layers)),
            {
                "id": "shoes_recovered",
                "name": "footwear",
                "image": {"path": f"parts/{sp.name}", "width": ssz[0], "height": ssz[1]},
                "rect": srect,
                "tags": ["foot"],
                "visible": True,
                "opacity": 1.0,
            },
        )
    if hat_rec is not None:
        hp, hsz, hrect = hat_rec
        insert_idx = next((i for i, lay in enumerate(layers) if lay["name"] == "headwear"), len(layers))
        layers.insert(
            insert_idx,
            {
                "id": "hat_recovered",
                "name": "headwear hat",
                "image": {"path": f"parts/{hp.name}", "width": hsz[0], "height": hsz[1]},
                "rect": hrect,
                "tags": ["headwear"],
                "visible": True,
                "opacity": 1.0,
            },
        )

    manifest: dict[str, Any] = {
        "format": "psd2live-layout",
        "version": 1,
        "name": name,
        "canvas": {"width": res, "height": res},
        "layers": layers,
    }
    (out_dir / "layout.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


# ---------- Web viewer upgrade & MOC3 validation ----------


def build_web_viewer_html(web_ui: Mapping[str, Any]) -> str:
    """Build export_web/index.html with independent ON/OFF prop/pose toggles."""
    title = html.escape(str(web_ui.get("title", "Live2D Viewer")))
    cfg_json = json.dumps(dict(web_ui), ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>{title}</title>
<style>
  html, body {{ margin: 0; height: 100%; background: #f4f4f4; font: 14px system-ui, sans-serif; color: #222; }}
  main {{ display: flex; flex-direction: column; height: 100%; }}
  canvas {{ flex: 1; width: 100%; min-height: 0; touch-action: none; }}
  nav {{ display: flex; gap: 10px 14px; align-items: center; padding: 8px 12px; flex-wrap: wrap; border-top: 1px solid #ddd; background: #fafafa; }}
  .toggles {{ display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }}
  .toggle-btn {{ padding: 4px 10px; border: 1px solid #bbb; border-radius: 6px; background: #fff; cursor: pointer; font: inherit; color: #222; }}
  .toggle-btn[aria-pressed="true"] {{ background: #2b4c7e; color: #fff; border-color: #2b4c7e; }}
  .sep {{ width: 1px; height: 20px; background: #d0d0d0; }}
  #error {{ color: #b00020; }}
</style>
</head>
<body>
<main>
  <canvas id="stage"></canvas>
  <nav>
    <label>Clip <select id="clip"><option value="">(none)</option></select></label>
    <label>Expression <select id="expression"><option value="">(none)</option></select></label>
    <span class="sep"></span>
    <div id="toggles" class="toggles"></div>
    <span class="sep"></span>
    <label>Mouth <input id="mouth" type="range" min="0" max="1" step="0.01" value="0"></label>
    <label id="advanced-label" hidden><input id="advanced" type="checkbox"> Advanced (live skin and cloth)</label>
    <span id="hit"></span>
    <span id="error"></span>
  </nav>
</main>
<script type="module">
import {{ P2LPlayer }} from "./p2l.js";

const WEB_UI = {cfg_json};
const canvas = document.getElementById("stage");
try {{
  const player = await P2LPlayer.create(canvas, "p2l_runtime.wasm", "layout.p2lrt");
  const select = document.getElementById("clip");
  for (const id of player.clips) select.add(new Option(id, id));
  select.onchange = () => player.play(select.value || null);

  const hideInExpr = new Set(WEB_UI.prop_expression_ids || []);
  const expression = document.getElementById("expression");
  for (const id of player.expressions) {{
    if (!hideInExpr.has(id)) expression.add(new Option(id, id));
  }}
  expression.onchange = () => player.expression(expression.value || null);

  const togglesEl = document.getElementById("toggles");
  const buttons = [];
  const activeState = new Map();
  const enforceRequires = () => {{
    let changed = true;
    while (changed) {{
      changed = false;
      for (const t of WEB_UI.toggles || []) {{
        if (t.requires && activeState.get(t.id) && !activeState.get(t.requires)) {{
          activeState.set(t.id, false);
          changed = true;
        }}
      }}
    }}
  }};
  const applyParams = () => {{
    enforceRequires();
    const paramVals = new Map();
    for (const t of WEB_UI.toggles || []) {{
      if (!paramVals.has(t.parameter)) paramVals.set(t.parameter, t.off_value);
    }}
    for (const t of WEB_UI.toggles || []) {{
      if (activeState.get(t.id)) paramVals.set(t.parameter, t.on_value);
    }}
    for (const [pid, val] of paramVals.entries()) {{
      player.setParameter(pid, val);
    }}
  }};
  const syncButtons = () => {{
    enforceRequires();
    for (const [b, spec] of buttons) {{
      b.setAttribute("aria-pressed", activeState.get(spec.id) ? "true" : "false");
    }}
  }};
  for (const t of WEB_UI.toggles || []) {{
    activeState.set(t.id, false);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toggle-btn";
    btn.textContent = t.label;
    btn.setAttribute("aria-pressed", "false");
    btn.onclick = () => {{
      const next = !activeState.get(t.id);
      if (next) {{
        const visited = new Set([t.id]);
        let reqId = t.requires;
        while (reqId && !visited.has(reqId)) {{
          visited.add(reqId);
          activeState.set(reqId, true);
          const reqSpec = (WEB_UI.toggles || []).find((x) => x.id === reqId);
          if (reqSpec && reqSpec.exclusive_group) {{
            for (const other of WEB_UI.toggles || []) {{
              if (other.id !== reqSpec.id && other.exclusive_group === reqSpec.exclusive_group) {{
                activeState.set(other.id, false);
              }}
            }}
          }}
          reqId = reqSpec ? reqSpec.requires : null;
        }}
      }}
      if (next && t.exclusive_group) {{
        for (const other of WEB_UI.toggles || []) {{
          if (other.id !== t.id && other.exclusive_group === t.exclusive_group) {{
            activeState.set(other.id, false);
          }}
        }}
      }}
      activeState.set(t.id, next);
      syncButtons();
      applyParams();
    }};
    buttons.push([btn, t]);
    togglesEl.appendChild(btn);
  }}
  const origUpdate = player.update.bind(player);
  player.update = (dt) => {{
    applyParams();
    origUpdate(dt);
  }};

  if (player.advancedFeatures) {{
    document.getElementById("advanced-label").hidden = false;
    document.getElementById("advanced").onchange = (e) => player.setAdvanced(e.target.checked ? player.advancedFeatures : 0);
  }}
  canvas.onpointerdown = (e) => {{ document.getElementById("hit").textContent = player.hitTest(e.clientX, e.clientY) ?? ""; }};
  document.getElementById("mouth").oninput = (e) => player.lipSync(Number(e.target.value));
  canvas.onpointermove = (e) => {{
    const r = canvas.getBoundingClientRect();
    player.lookAt(((e.clientX - r.left) / r.width) * 2 - 1, 1 - ((e.clientY - r.top) / r.height) * 2);
  }};
  canvas.onpointerleave = () => player.lookAt(0, 0);
  document.documentElement.dataset.p2lReady = "1";
}} catch (e) {{
  document.getElementById("error").textContent = String(e) + (location.protocol === "file:" ? " (serve this folder over http)" : "");
  document.documentElement.dataset.p2lReady = "error";
}}
</script>
</body>
</html>
"""


def patch_web_viewer(index_path: Path, web_ui: Mapping[str, Any]) -> Path:
    """Write upgraded independent-toggle viewer HTML to index_path."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(build_web_viewer_html(web_ui), encoding="utf-8")
    return index_path


def validate_moc3_dir(moc3_dir: Path) -> dict[str, Any]:
    """Validate *.moc3 binary header and *.model3.json references in moc3_dir."""
    result: dict[str, Any] = {"dir": str(moc3_dir), "files": [], "moc3": None, "model3": None}
    if not moc3_dir.is_dir():
        result["error"] = "directory missing"
        return result
    files = sorted(p.relative_to(moc3_dir).as_posix() for p in moc3_dir.rglob("*") if p.is_file())
    result["files"] = files
    moc_files = sorted(moc3_dir.rglob("*.moc3"))
    if moc_files:
        moc_path = moc_files[0]
        raw = moc_path.read_bytes()
        magic = raw[:4].decode("ascii", errors="replace")
        ver = raw[4] if len(raw) > 4 else None
        is_big_endian = raw[5] if len(raw) > 5 else None
        result["moc3"] = {
            "path": moc_path.relative_to(moc3_dir).as_posix(),
            "bytes": len(raw),
            "magic": magic,
            "version": ver,
            "big_endian": bool(is_big_endian),
            "header_ok": magic == "MOC3" and ver in (1, 2, 3, 4, 5, 6),
        }
    m3_files = sorted(moc3_dir.rglob("*.model3.json"))
    if m3_files:
        m3 = json.loads(m3_files[0].read_text(encoding="utf-8"))
        refs = m3.get("FileReferences", {})
        result["model3"] = {
            "path": m3_files[0].relative_to(moc3_dir).as_posix(),
            "version": m3.get("Version"),
            "moc": refs.get("Moc"),
            "textures": refs.get("Textures", []),
            "physics": refs.get("Physics"),
            "display_info": refs.get("DisplayInfo"),
            "expressions": [e.get("Name") for e in refs.get("Expressions", [])],
            "motions": {k: len(v) for k, v in refs.get("Motions", {}).items()},
            "groups": [g.get("Name") for g in m3.get("Groups", [])],
        }
    return result


# ---------- PSD2Live Headless MCP Runner ----------


def discover_p2l_dir(explicit: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Resolve PSD2Live portable directory from --p2l-dir or PSD2LIVE_DIR env var."""
    env_map = os.environ if env is None else env
    raw = explicit if explicit is not None else (Path(env_map["PSD2LIVE_DIR"]) if env_map.get("PSD2LIVE_DIR") else None)
    if raw is None:
        raise FileNotFoundError(
            "PSD2Live directory not configured. Pass --p2l-dir <path> or set PSD2LIVE_DIR."
        )
    resolved = raw.resolve()
    if not (resolved / "app").is_dir():
        raise FileNotFoundError(f"PSD2Live app directory missing under {resolved}")
    return resolved


def build_p2l_mcp_cmd(
    p2l_dir: Path,
    port: int,
    token: str,
    store_dir: Path,
    *,
    app_version: str = "3.8.4",
) -> list[str]:
    """Build cross-platform Java command line for PSD2Live headless MCP server."""
    app_dir = p2l_dir / "app"
    win_java = p2l_dir / "runtime" / "bin" / "java.exe"
    posix_java = p2l_dir / "runtime" / "bin" / "java"
    if win_java.exists():
        java_exe = str(win_java)
    elif posix_java.exists():
        java_exe = str(posix_java)
    else:
        java_exe = "java"
    return [
        java_exe,
        f"-Djpackage.app-version={app_version}",
        f"-Dcompose.application.resources.dir={app_dir / 'resources'}",
        "-Dfile.encoding=UTF-8",
        f"-Dskiko.library.path={app_dir}",
        "-cp",
        str(app_dir / "*"),
        "io.github.psd2live.MainKt",
        "mcp",
        "--port",
        str(port),
        "--token",
        token,
        "--store",
        str(store_dir),
    ]


def wait_for_mcp(
    port: int,
    proc: Any,
    timeout: float = 30.0,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.time,
) -> None:
    """Poll local PSD2Live MCP endpoint until HTTP responds or timeout expires."""
    deadline = time_fn() + timeout
    url = f"http://127.0.0.1:{port}/mcp"
    while time_fn() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"PSD2Live MCP exited early with code {proc.returncode}")
        try:
            opener(url, timeout=1.0)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            sleep_fn(0.4)
    raise TimeoutError(f"PSD2Live MCP did not start on port {port} within {timeout}s")


def _default_client_factory(p2l_dir: Path, endpoint: str, token: str) -> Any:
    p2l_str = str(p2l_dir)
    if p2l_str not in sys.path:
        sys.path.insert(0, p2l_str)
    mcp_proxy = importlib.import_module("mcp_proxy")
    return mcp_proxy.Client(endpoint=endpoint, token=token, timeout=300.0)


def apply_post_rig(
    p2l: Any,
    recipe: Mapping[str, Any],
    *,
    expressions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply ordered post-rig recipe onto an open PSD2Live MCP session.

    Preserves strict PSD2Live ordering invariants:
    1) settings/modifiers -> 2) name/default updates -> 3) deformer rebinds &
    rig_create_warp -> 4) Phase A author_axes (non-clamped) -> 5) keyform_apply ->
    6) motion_scale -> 7) expression_put -> 8) parameter range clamps ->
    9) canvas_warps -> 10) Phase B author_axes (clamped) -> 11) physics & swings.
    """
    warnings: list[str] = []

    if recipe.get("settings_update"):
        p2l.edit("settings_update", changes=recipe["settings_update"])

    mod_updates = recipe.get("modifier_updates", {})
    dis_types = set(recipe.get("disable_modifiers", []))
    if mod_updates or dis_types:
        st0, _ = p2l.call("modifier_stack", {})
        for m in st0.get("entries") or st0.get("modifiers") or st0.get("items") or []:
            mtype = m.get("type")
            if mtype in dis_types:
                p2l.edit("modifier_enable", id=m["id"], enabled=False)
            if mtype in mod_updates:
                cfg = json.loads(json.dumps(m.get("config") or {}))
                for pk, pv in mod_updates[mtype].items():
                    if isinstance(pv, dict) and isinstance(cfg.get(pk), dict):
                        cfg[pk].update(pv)
                    else:
                        cfg[pk] = pv
                p2l.edit("modifier_update", id=m["id"], type=mtype, config=cfg)

    ov0, _ = p2l.call("workspace_overview", {"limit": 500})
    all_objs0: list[dict[str, Any]] = []
    offset = 0
    while True:
        page, _ = p2l.call("workspace_inspect", {"scope": "objects", "limit": 64, "offset": offset})
        items = page.get("items", [])
        all_objs0.extend(items)
        if len(items) < 64 or len(all_objs0) >= page.get("total", 0):
            break
        offset += len(items)

    params0 = {p["id"]: p for p in ov0.get("parameters", {}).get("items", [])}
    by_lid = {it.get("layerId"): it for it in all_objs0 if it.get("layerId")}
    deformers_by_name = {df.get("name"): df.get("target") for df in ov0.get("deformers", {}).get("items", [])}

    clamped_pids = {
        pid
        for pid, cfg in recipe.get("parameter_defaults", {}).items()
        if pid in params0 and ("min" in cfg or "max" in cfg)
    }
    for pid, cfg in recipe.get("parameter_defaults", {}).items():
        if pid in params0:
            req: dict[str, Any] = {"parameter_id": pid}
            for key in ("name", "default"):
                if key in cfg:
                    req[key] = cfg[key]
            if len(req) > 1:
                p2l.edit("parameter_update", **req)

    def resolve_target(tgt: str) -> str | None:
        if tgt.startswith("layer:"):
            lid = tgt.split(":", 1)[1]
            if lid in by_lid:
                return by_lid[lid]["target"]
            warnings.append(f"target layer_id '{lid}' not found")
            return None
        if tgt.startswith("deformer:"):
            dname = tgt.split(":", 1)[1]
            if dname in deformers_by_name:
                return deformers_by_name[dname]
            warnings.append(f"target deformer '{dname}' not found")
            return None
        return tgt

    bind_edits = []
    for bspec in recipe.get("bind_to_deformer", []):
        dtarget = deformers_by_name.get(bspec["deformer_name"], "")
        if not dtarget:
            warnings.append(f"deformer '{bspec['deformer_name']}' not found")
            continue
        did = dtarget.split(":", 1)[1] if dtarget.startswith(("warp:", "rotation:")) else dtarget
        for lid in bspec.get("layer_ids", []):
            if lid in by_lid:
                mt = by_lid[lid]["target"]
                mid = mt.split(":", 1)[1] if mt.startswith("mesh:") else mt
                bind_edits.append(
                    {
                        "action": "bind",
                        "kind": "mesh",
                        "id": mid,
                        "parent_id": did,
                        "space": bspec.get("space", "canvas"),
                    }
                )
            else:
                warnings.append(f"bind layer_id '{lid}' not found")
    if bind_edits:
        p2l.edit("object_edit_appearance", edits=bind_edits)

    for wspec in recipe.get("create_warps", []):
        w_targets = []
        for lid in wspec.get("layer_ids", []):
            if lid in by_lid:
                w_targets.append(by_lid[lid]["target"])
            else:
                warnings.append(f"warp target layer_id '{lid}' not found")
        for did_name in wspec.get("deformer_ids", []):
            if did_name in deformers_by_name:
                w_targets.append(deformers_by_name[did_name])
            else:
                warnings.append(f"warp target deformer '{did_name}' not found")
        if w_targets:
            wres = p2l.edit(
                "rig_create_warp",
                id=wspec["id"],
                name=wspec.get("name", wspec["id"]),
                targets=w_targets,
                rows=int(wspec.get("rows", 3)),
                columns=int(wspec.get("columns", 4)),
                fit_local=bool(wspec.get("fit_local", True)),
            )
            wtarget = wres.get("target") or f"warp:{wspec['id']}"
            deformers_by_name[wspec["id"]] = wtarget
            deformers_by_name[wspec.get("name", wspec["id"])] = wtarget

    def apply_author_axis(ax: Mapping[str, Any]) -> None:
        shapes = []
        for sh in ax.get("shapes", []):
            resolved = resolve_target(sh["target"])
            if resolved is None:
                continue
            sh_copy = json.loads(json.dumps(sh))
            sh_copy["target"] = resolved
            shapes.append(sh_copy)
        if not shapes:
            return
        p2l.edit(
            "author_axis",
            summary=ax.get("summary", "Author custom axis"),
            parameter=ax["parameter"],
            shapes=shapes,
        )

    for ax in recipe.get("author_axes", []):
        if ax["parameter"]["id"] not in clamped_pids:
            apply_author_axis(ax)

    kf_changes = []
    for ch_item in recipe.get("keyform_applies", []):
        resolved = resolve_target(ch_item["target"])
        if resolved is None:
            continue
        c_copy = json.loads(json.dumps(ch_item))
        c_copy["target"] = resolved
        kf_changes.append(c_copy)
    if kf_changes:
        for i in range(0, len(kf_changes), 30):
            p2l.edit("keyform_apply", changes=kf_changes[i : i + 30])

    if recipe.get("motion_scale"):
        mscale = recipe["motion_scale"]
        scale_params = mscale.get("parameters", {})
        target_clips = set(mscale.get("clips", []))
        mlist, _ = p2l.call("motion_list", {})
        for clip_meta in mlist.get("clips") or mlist.get("items") or []:
            cname = clip_meta.get("name", "")
            cid = clip_meta.get("id", "")
            if target_clips and cname not in target_clips:
                continue
            cdet, _ = p2l.call("motion_get", {"id": cid})
            clip_obj = cdet.get("clip") or cdet
            curves = clip_obj.get("curves") or []
            changed = False
            new_curves = []
            for cv in curves:
                cv_c = json.loads(json.dumps(cv))
                pid = cv_c.get("parameter")
                if pid in scale_params:
                    fac = float(scale_params[pid])
                    pcfg = recipe.get("parameter_defaults", {}).get(pid, {})
                    pmin = float(pcfg["min"]) if "min" in pcfg else -1e9
                    pmax = float(pcfg["max"]) if "max" in pcfg else 1e9
                    for k in cv_c.get("keys", []):
                        k["value"] = round(max(pmin, min(pmax, float(k["value"]) * fac)), 4)
                        if isinstance(k.get("in"), list) and len(k["in"]) == 2:
                            k["in"][1] = round(max(pmin, min(pmax, float(k["in"][1]) * fac)), 4)
                        if isinstance(k.get("out"), list) and len(k["out"]) == 2:
                            k["out"][1] = round(max(pmin, min(pmax, float(k["out"][1]) * fac)), 4)
                    changed = True
                new_curves.append(cv_c)
            if changed:
                put_clip = {
                    k: clip_obj[k]
                    for k in ("id", "name", "builtin", "loop", "duration", "fps", "fade_in", "fade_out", "enabled")
                    if k in clip_obj and clip_obj[k] is not None
                }
                put_clip["curves"] = new_curves
                p2l.edit("motion_put", clip=put_clip)

    if expressions:
        for ex in expressions:
            p2l.edit("expression_put", expression=ex)

    for pid, cfg in recipe.get("parameter_defaults", {}).items():
        if pid in clamped_pids:
            req = {"parameter_id": pid}
            for key in ("min", "max", "default"):
                if key in cfg:
                    req[key] = cfg[key]
            p2l.edit("parameter_update", **req)

    for cwspec in recipe.get("canvas_warps", []):
        mesh_ids = []
        for lid in cwspec.get("layer_ids", []):
            if lid in by_lid:
                mt = by_lid[lid]["target"]
                mesh_ids.append(mt.split(":", 1)[1] if mt.startswith("mesh:") else mt)
            else:
                warnings.append(f"canvas_warp layer_id '{lid}' not found")
        req = {
            "id": cwspec["id"],
            "name": cwspec.get("name", cwspec["id"]),
            "meshes": mesh_ids,
            "add_to": cwspec.get("add_to", "parent_of_selected"),
            "size_strategy": cwspec.get("size_strategy", "selection_bounds"),
            "rows": int(cwspec.get("rows", 3)),
            "columns": int(cwspec.get("columns", 5)),
        }
        if cwspec.get("parent_deformer"):
            pt = deformers_by_name.get(cwspec["parent_deformer"], "")
            if not pt:
                warnings.append(f"canvas_warp parent_deformer '{cwspec['parent_deformer']}' not found")
                continue
            req["add_to"] = "specify_parent"
            req["parent_id"] = pt.split(":", 1)[1] if pt.startswith(("warp:", "rotation:")) else pt
        cwres = p2l.edit("canvas_warp", **req)
        wid = cwres.get("id") or cwspec["id"]
        wtarget = f"warp:{wid}"
        deformers_by_name[cwspec["id"]] = wtarget
        deformers_by_name[cwspec.get("name", cwspec["id"])] = wtarget

    for ax in recipe.get("author_axes", []):
        if ax["parameter"]["id"] in clamped_pids:
            apply_author_axis(ax)

    for pm in recipe.get("physics_modifiers", []):
        pm_cfg = {k: v for k, v in pm.items() if k != "layer_ids"}
        pm_targets = []
        for lid in pm.get("layer_ids", []):
            if lid in by_lid:
                mt = by_lid[lid]["target"]
                mid = mt.split(":", 1)[1] if mt.startswith("mesh:") else mt
                pm_targets.append({"kind": "mesh", "id": mid})
            else:
                warnings.append(f"physics_modifier layer_id '{lid}' not found")
        p2l.edit("modifier_add", type="physics", targets=pm_targets, config=pm_cfg)

    for sw in recipe.get("swings", []):
        lid = sw["layer_id"]
        if lid in by_lid:
            mt = by_lid[lid]["target"]
            mid = mt.split(":", 1)[1] if mt.startswith("mesh:") else mt
            p2l.edit(
                "swing_put",
                id=sw["id"],
                name=sw.get("name", sw["id"]),
                targets=[mid],
                fulcrum=sw.get("fulcrum", "top"),
                preset=sw.get("preset", "accessory"),
                physics_enabled=True,
                motions=sw["motions"],
            )
        else:
            warnings.append(f"swing layer_id '{lid}' not found")

    return {"rebound": len(bind_edits), "keyforms": len(kf_changes), "warnings": warnings}


def run_p2l_pipeline(
    *,
    out_dir: Path,
    name: str,
    p2l_dir: Path,
    layout_path: Path | None = None,
    psd_path: Path | None = None,
    preset: str = "modern-standard",
    port: int = 23881,
    face_rect: str = "",
    expressions_path: Path | None = None,
    extra_poses_path: Path | None = None,
    post_rig_path: Path | None = None,
    sabun_poses_path: Path | None = None,
    targets: tuple[tuple[str, dict[str, str]], ...] = DEFAULT_EXPORT_TARGETS,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    wait_fn: Callable[..., None] = wait_for_mcp,
    client_factory: Callable[[Path, str, str], Any] | None = None,
) -> dict[str, Any]:
    """Run full headless PSD2Live MCP import -> rig -> post-rig -> poses -> export."""
    if (layout_path is None) == (psd_path is None):
        raise ValueError("Provide exactly one of layout_path or psd_path")

    for label, opt_path in (
        ("layout_path", layout_path),
        ("psd_path", psd_path),
        ("expressions_path", expressions_path),
        ("post_rig_path", post_rig_path),
        ("extra_poses_path", extra_poses_path),
        ("sabun_poses_path", sabun_poses_path),
    ):
        if opt_path is not None and not opt_path.is_file():
            raise FileNotFoundError(f"{label} not found: {opt_path}")

    parsed_face_rect: tuple[float, float, float, float] | None = None
    if face_rect:
        parts = [float(x) for x in face_rect.split(",")]
        if len(parts) != 4:
            raise ValueError("face_rect must be 4 comma-separated numbers: left,top,width,height")
        parsed_face_rect = (parts[0], parts[1], parts[2], parts[3])

    out_resolved = out_dir.resolve()
    out_resolved.mkdir(parents=True, exist_ok=True)
    store_dir = out_resolved / "_mcp_store"
    if store_dir.exists():
        shutil.rmtree(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)

    token = f"p2l_{secrets.token_hex(16)}"
    cmd = build_p2l_mcp_cmd(p2l_dir.resolve(), port, token, store_dir)
    popen_kwargs: dict[str, Any] = {"text": True}
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if no_window:
        popen_kwargs["creationflags"] = no_window

    factory = client_factory or _default_client_factory
    t0 = time.perf_counter()
    with (
        open(out_resolved / "_mcp_stdout.log", "w", encoding="utf-8") as log_out,
        open(out_resolved / "_mcp_stderr.log", "w", encoding="utf-8") as log_err,
    ):
        proc = popen_fn(cmd, stdout=log_out, stderr=log_err, **popen_kwargs)
        try:
            wait_fn(port, proc)
            endpoint = f"http://127.0.0.1:{port}/mcp"
            with factory(p2l_dir.resolve(), endpoint, token) as p2l:
                if layout_path is not None:
                    p2l.edit("project_import_layout", path=str(layout_path.resolve()), discard_unsaved=True)
                    p2l.edit("rig_generate", preset_group=preset)
                else:
                    assert psd_path is not None
                    p2l.edit(
                        "project_import_psd",
                        path=str(psd_path.resolve()),
                        discard_unsaved=True,
                        modifiers="default",
                    )

                exprs = (
                    json.loads(expressions_path.read_text(encoding="utf-8"))
                    if expressions_path is not None
                    else None
                )
                recipe = (
                    json.loads(post_rig_path.read_text(encoding="utf-8"))
                    if post_rig_path is not None
                    else None
                )
                post_summary = None
                if recipe is not None:
                    post_summary = apply_post_rig(p2l, recipe, expressions=exprs)
                elif exprs:
                    for ex in exprs:
                        p2l.edit("expression_put", expression=ex)

                ov, _ = p2l.call("workspace_overview", {"limit": 500})
                proj, _ = p2l.call("workspace_inspect", {"scope": "project"})
                layers_insp, _ = p2l.call("workspace_inspect", {"scope": "layers"})
                (out_resolved / "overview.json").write_text(
                    json.dumps(
                        {
                            "overview": ov,
                            "project": proj,
                            "layers": layers_insp,
                            "post_rig_summary": post_summary,
                            "elapsed_rig_s": round(time.perf_counter() - t0, 2),
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                canvas = ov.get("canvas", [1200, 2400])
                if isinstance(canvas, list) and len(canvas) >= 2:
                    cw, ch = float(canvas[0]), float(canvas[1])
                elif isinstance(canvas, dict):
                    cw, ch = float(canvas.get("width", 1200)), float(canvas.get("height", 2400))
                else:
                    cw, ch = 1200.0, 2400.0

                full_poses = [
                    {},
                    {"ParamAngleX": -25.0},
                    {"ParamAngleX": 25.0},
                    {"ParamAngleY": 18.0},
                    {"ParamBodyAngleX": 10.0, "ParamAngleZ": 12.0},
                    {"ParamEyeLOpen": 0.0, "ParamEyeROpen": 0.0, "ParamMouthOpenY": 0.0},
                ]
                _, imgs = p2l.call(
                    "view_render_poses",
                    {
                        "viewport": {"mode": "canvas_rect", "left": 0, "top": 0, "width": cw, "height": ch},
                        "poses": full_poses,
                        "columns": 3,
                        "background": "checkerboard",
                        "target_long_edge": 1600,
                    },
                )
                if imgs:
                    (out_resolved / "poses_full.png").write_bytes(imgs[0])

                if parsed_face_rect is not None:
                    fl, ft, fw, fh = parsed_face_rect
                else:
                    fw, fh = cw * 0.60, ch * 0.30
                    fl, ft = (cw - fw) * 0.5, ch * 0.02

                face_poses: list[dict[str, float]] = [
                    {},
                    {"ParamEyeLOpen": 0.0, "ParamEyeROpen": 0.0},
                    {"ParamMouthOpenY": 0.0},
                    {"ParamMouthOpenY": 1.0},
                    {"ParamAngleX": -25.0, "ParamAngleY": 10.0},
                    {"ParamAngleX": 25.0, "ParamAngleY": -10.0},
                ]
                if extra_poses_path is not None:
                    extra = json.loads(extra_poses_path.read_text(encoding="utf-8"))
                    face_poses = (face_poses[:3] + extra)[:9]

                _, fimgs = p2l.call(
                    "view_render_poses",
                    {
                        "viewport": {"mode": "canvas_rect", "left": fl, "top": ft, "width": fw, "height": fh},
                        "poses": face_poses,
                        "columns": 3,
                        "background": "checkerboard",
                        "target_long_edge": 1600,
                    },
                )
                if fimgs:
                    (out_resolved / "poses_face.png").write_bytes(fimgs[0])

                if sabun_poses_path is not None:
                    sp = json.loads(sabun_poses_path.read_text(encoding="utf-8"))
                    vp = sp["viewport"]
                    all_p = sp["poses"]
                    chunk_size = 8
                    for idx in range(0, len(all_p), chunk_size):
                        chunk = all_p[idx : idx + chunk_size]
                        _, simgs = p2l.call(
                            "view_render_poses",
                            {
                                "viewport": {"mode": "canvas_rect", **vp},
                                "poses": chunk,
                                "columns": min(3, int(sp.get("columns", 3))),
                                "background": "checkerboard",
                                "target_long_edge": 2048,
                            },
                        )
                        if simgs:
                            part_i = idx // chunk_size + 1
                            fname = "poses_sabun.png" if len(all_p) <= chunk_size else f"poses_sabun_{part_i}.png"
                            (out_resolved / fname).write_bytes(simgs[0])

                p2l.edit("project_save_as", path=str(out_resolved / f"{name}.psd2live"))

                export_results: dict[str, Any] = {}
                for target, settings in targets:
                    tdir = out_resolved / f"export_{target}"
                    if tdir.exists():
                        shutil.rmtree(tdir)
                    tdir.mkdir(parents=True, exist_ok=True)
                    try:
                        eres = p2l.edit(
                            "project_export_target",
                            target=target,
                            output_directory=str(tdir),
                            settings=settings,
                        )
                        export_results[target] = {"ok": True, "result": eres}
                    except Exception as exc:
                        export_results[target] = {"ok": False, "error": str(exc)}

                if recipe and recipe.get("web_ui"):
                    web_index = out_resolved / "export_web" / "index.html"
                    if web_index.exists():
                        patch_web_viewer(web_index, recipe["web_ui"])

                moc_check = validate_moc3_dir(out_resolved / "export_moc3")
                vts_check = validate_moc3_dir(out_resolved / "export_vtube-studio")
                report = {
                    "name": name,
                    "elapsed_total_s": round(time.perf_counter() - t0, 2),
                    "post_rig_summary": post_summary,
                    "exports": export_results,
                    "moc3_validation": moc_check,
                    "vts_validation": vts_check,
                }
                (out_resolved / "export_report.json").write_text(
                    json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                return report
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------- CLI ----------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live2d_pipeline.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("recover-single", help="Build Case 2B layout from transparent PNG + See-through PSD")
    p_rec.add_argument("src_rgba", type=Path)
    p_rec.add_argument("seethrough_psd", type=Path)
    p_rec.add_argument("-o", "--out", type=Path, required=True)
    p_rec.add_argument("--name", default="model")
    p_rec.add_argument("--no-reproject", action="store_true")

    p_eye = sub.add_parser("split-eyes", help="Split bilateral eye/brow PNG into right/left parts")
    p_eye.add_argument("src_png", type=Path)
    p_eye.add_argument("-o", "--out", type=Path, required=True)
    p_eye.add_argument("--prefix", required=True)
    p_eye.add_argument("--rect", required=True, help="left,top,width,height on canvas")

    p_run = sub.add_parser("run-p2l", help="Run headless PSD2Live MCP rig + export pipeline")
    src = p_run.add_mutually_exclusive_group(required=True)
    src.add_argument("--layout", type=Path)
    src.add_argument("--psd", type=Path)
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument("--name", required=True)
    p_run.add_argument("--p2l-dir", type=Path, default=None)
    p_run.add_argument("--preset", default="modern-standard")
    p_run.add_argument("--port", type=int, default=23881)
    p_run.add_argument("--face-rect", default="")
    p_run.add_argument("--expressions", type=Path, default=None)
    p_run.add_argument("--extra-poses", type=Path, default=None)
    p_run.add_argument("--post-rig", type=Path, default=None)
    p_run.add_argument("--sabun-poses", type=Path, default=None)

    p_web = sub.add_parser("patch-web", help="Upgrade export_web/index.html with independent toggles")
    p_web.add_argument("index_html", type=Path)
    p_web.add_argument("--config", type=Path, required=True)

    p_val = sub.add_parser("validate-moc3", help="Validate exported .moc3 header and .model3.json")
    p_val.add_argument("moc3_dir", type=Path)
    p_val.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "recover-single":
        manifest = build_single_image_layout(
            args.src_rgba,
            args.seethrough_psd,
            args.out,
            name=args.name,
            reproject=not args.no_reproject,
        )
        print(json.dumps({"name": manifest["name"], "layer_count": len(manifest["layers"])}, ensure_ascii=False))
        return 0

    if args.cmd == "split-eyes":
        nums = [float(x) for x in args.rect.split(",")]
        if len(nums) != 4:
            print("ERROR: --rect must be left,top,width,height", file=sys.stderr)
            return 2
        rect = {"left": nums[0], "top": nums[1], "width": nums[2], "height": nums[3]}
        res = split_eyes_and_brows(args.src_png, args.out, args.prefix, rect)
        print(json.dumps({k: {"path": str(v[0]), "size": v[1], "rect": v[2]} for k, v in res.items()}))
        return 0

    if args.cmd == "patch-web":
        cfg = json.loads(args.config.read_text(encoding="utf-8"))
        patch_web_viewer(args.index_html, cfg)
        print(f"Patched {args.index_html}")
        return 0

    if args.cmd == "validate-moc3":
        res = validate_moc3_dir(args.moc3_dir)
        print(json.dumps(res, indent=2 if not args.json else None, ensure_ascii=False))
        ok = bool(res.get("moc3") and res["moc3"].get("header_ok"))
        return 0 if ok else 1

    # run-p2l
    p2l_dir = discover_p2l_dir(args.p2l_dir)
    report = run_p2l_pipeline(
        out_dir=args.out,
        name=args.name,
        p2l_dir=p2l_dir,
        layout_path=args.layout,
        psd_path=args.psd,
        preset=args.preset,
        port=args.port,
        face_rect=args.face_rect,
        expressions_path=args.expressions,
        extra_poses_path=args.extra_poses,
        post_rig_path=args.post_rig,
        sabun_poses_path=args.sabun_poses,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    all_exports_ok = all(item.get("ok") for item in report.get("exports", {}).values())
    moc = report.get("moc3_validation", {}).get("moc3")
    return 0 if (all_exports_ok and moc and moc.get("header_ok")) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
