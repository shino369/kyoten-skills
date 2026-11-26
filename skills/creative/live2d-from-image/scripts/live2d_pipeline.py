#!/usr/bin/env python3
"""live2d_pipeline: reusable image-to-Live2D (.moc3 / VTS / WebGL / GIF) CLI.

Subcommands (run with uv, nothing installed globally):
  recover-single  Build a Case 2B psd2live-layout manifest from a transparent
                  illustration + See-through PSD (with native-pixel
                  re-projection at full source resolution and alpha-residual
                  headwear/footwear recovery).
  split-eyes      Split a bilateral eye/brow PNG into right/left eye & brow PNGs.
  run-p2l         Run PSD2Live headless MCP: import layout/PSD -> rig ->
                  ordered post_rig recipe -> expressions -> pose sheets ->
                  export moc3/vtube-studio/web/gif -> validate exports & losses.
  patch-web       Upgrade export_web/index.html with independent ON/OFF
                  prop/pose toggles combinable with any facial expression.
  validate-moc3   Inspect *.moc3 header, *.model3.json references, and physics3.

Run it through the `l2d` helper in SKILL.md from a folder you trust, never from inside a folder
of received artwork: uv reads pyproject.toml, uv.toml, and .venv from the folder it runs in.

  uv run --no-project --no-config --no-build --exclude-newer P7D --python 3.12 \\
    --with pillow==13.0.0 --with numpy==2.5.5 --with scipy==1.18.1 \\
    --with psd-tools==1.27.0 --with attrs==26.1.0 \\
    python -I live2d_pipeline.py validate-moc3 "<out_dir>/export_moc3" --json
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
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

REQUIRED_MODEL3_GROUPS: tuple[str, ...] = ("EyeBlink", "LipSync")


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
    brow_max_h_ratio: float = 0.34,
    dust_ratio: float = 0.1,
    warnings: list[str] | None = None,
) -> dict[str, tuple[Path, tuple[int, int], dict[str, float]]]:
    """Split a bilateral eye PNG into character right/left eyes and right/left brows.

    Uses per-side relative geometry with dust-speck filtering and per-column
    vertical gap checks so three-quarter views and curved anime brows whose
    outer tips dip below the eye arch classify accurately. A component smaller than
    ``dust_ratio`` of the feature it is compared with does not decide which part is
    the brow. Components that end up in neither the brow nor the eye are attached to
    the nearest of the two. Appends a warning to ``warnings`` whenever a side has an
    eye without a detected brow, or a component of at least ``dust_ratio`` of the brow
    seed had to be attached that way.

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

    by_side: dict[str, list[dict[str, Any]]] = {"r": [], "l": []}
    for i in range(1, n + 1):
        mask = labeled == i
        ys, xs = np.where(mask)
        area = len(xs)
        if area < min_px:
            continue
        cx = float(xs.mean())
        y_min, y_max = int(ys.min()), int(ys.max())
        side = "r" if cx < mid_x else "l"
        by_side[side].append(
            {
                "mask": mask,
                "area": area,
                "y_min": y_min,
                "y_max": y_max,
                "y_center": 0.5 * (y_min + y_max),
                "height": y_max - y_min + 1,
            }
        )

    buckets: dict[str, np.ndarray] = {
        "eye_r": np.zeros((h, w), dtype=bool),
        "eye_l": np.zeros((h, w), dtype=bool),
        "brow_r": np.zeros((h, w), dtype=bool),
        "brow_l": np.zeros((h, w), dtype=bool),
    }
    max_brow_h = int(h * brow_max_h_ratio)
    min_brow_gap = max(4, int(h * 0.04))

    def _strictly_below_on_shared_x(upper_mask: np.ndarray, comp_mask: np.ndarray) -> bool:
        shared = np.where(upper_mask.any(axis=0) & comp_mask.any(axis=0))[0]
        if len(shared) == 0:
            return False
        return all(
            int(np.where(comp_mask[:, x])[0].min()) > int(np.where(upper_mask[:, x])[0].max())
            for x in shared
        )

    for side, comps in by_side.items():
        if not comps:
            continue
        comps_sorted = sorted(comps, key=lambda c: (c["y_min"], c["y_center"]))
        largest = max(comps_sorted, key=lambda c: c["area"])
        below_largest = [c for c in comps_sorted if c["y_min"] - largest["y_max"] >= min_brow_gap]
        above_largest = comps_sorted[: next(i for i, c in enumerate(comps_sorted) if c is largest)]
        # Thick brow over a thin closed lid: the largest component is the brow when
        # nothing but dust lies above it and something sits below it.
        lid = max(below_largest, key=lambda c: c["area"]) if below_largest else None
        if (
            lid is not None
            and largest["height"] <= max_brow_h
            and all(c["area"] < dust_ratio * lid["area"] for c in above_largest)
        ):
            main_eye = lid
        else:
            main_eye = largest
        min_seed_area = max(min_px, int(main_eye["area"] * 0.015))
        seed_cands = [
            c
            for c in comps_sorted
            if c is not main_eye
            and c["area"] >= min_seed_area
            and c["height"] <= max_brow_h
            and c["y_min"] < main_eye["y_min"]
        ]
        seed: dict[str, Any] | None = None
        if seed_cands:
            # Topmost brow-like component, skipping dust relative to the biggest one.
            biggest_area = max(c["area"] for c in seed_cands)
            seed = next(c for c in seed_cands if c["area"] >= dust_ratio * biggest_area)
        brow_group: list[dict[str, Any]] = []
        if seed is not None:
            brow_band_max = seed["y_max"] + max(4, seed["height"])
            brow_group = [
                c
                for c in comps_sorted
                if c is not main_eye
                and c["height"] <= max_brow_h
                and c["y_max"] >= seed["y_min"] - 2
                and c["y_min"] <= seed["y_max"] + 2
                and c["y_max"] <= brow_band_max
                and not _strictly_below_on_shared_x(seed["mask"], c["mask"])
            ]
        brow_ids = {id(c) for c in brow_group}
        rest = [c for c in comps_sorted if id(c) not in brow_ids]
        eye_below: list[dict[str, Any]] = []
        gap_ok = False
        if brow_group:
            brow_mask = np.zeros((h, w), dtype=bool)
            for comp in brow_group:
                brow_mask |= comp["mask"]
            eye_below = [c for c in rest if c is main_eye or c["y_min"] >= seed["y_min"]]
            rest_mask = np.zeros((h, w), dtype=bool)
            for comp in eye_below:
                rest_mask |= comp["mask"]
            shared_x = np.where(brow_mask.any(axis=0) & rest_mask.any(axis=0))[0]
            if len(shared_x) > 0:
                gaps = [
                    int(np.where(rest_mask[:, x])[0].min()) - int(np.where(brow_mask[:, x])[0].max())
                    for x in shared_x
                ]
                gap_ok = float(np.median(gaps)) >= min_brow_gap and min(gaps) >= 1
            else:
                gap_ok = (min(c["y_min"] for c in eye_below) - max(c["y_max"] for c in brow_group)) >= min_brow_gap
        if gap_ok:
            for comp in brow_group:
                buckets[f"brow_{side}"] |= comp["mask"]
            for comp in eye_below:
                buckets[f"eye_{side}"] |= comp["mask"]
            placed = brow_ids | {id(c) for c in eye_below}
            leftovers = [c for c in comps_sorted if id(c) not in placed]
            if leftovers:
                dist = {
                    key: ndimage.distance_transform_edt(~buckets[key]) for key in (f"brow_{side}", f"eye_{side}")
                }
                for comp in leftovers:
                    key = min(dist, key=lambda k: float(dist[k][comp["mask"]].min()))
                    buckets[key] |= comp["mask"]
                    if warnings is not None and comp["area"] >= dust_ratio * seed["area"]:
                        warnings.append(
                            f"Side '{side}': {comp['area']} px were neither brow nor eye; attached to {key}"
                        )
        else:
            for comp in comps_sorted:
                buckets[f"eye_{side}"] |= comp["mask"]
            if warnings is not None:
                warnings.append(f"No separate brow component detected on side '{side}'")

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
    """Build a native-resolution psd2live-layout manifest from PNG + See-through PSD.

    ``min_top_px`` / ``min_bottom_px`` and the internal edge tolerances are in
    See-through grid pixels (``psd.width``); they are converted to native pixels so a
    1-2 grid-px segmentation edge mismatch on a larger source is not mistaken for a
    dropped part.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_parts = out_dir / "parts"
    out_parts.mkdir(parents=True, exist_ok=True)

    loader = psd_loader or _default_psd_loader
    psd = loader(seethrough_psd)
    res = int(psd.width)

    orig = Image.open(src_rgba).convert("RGBA")
    ow, oh = orig.size
    orig_arr = np.array(orig)
    scale, pad_x, pad_y = unpad_params(ow, oh, res)
    native_per_grid = 1.0 / scale

    def to_native_len(grid_px: int) -> int:
        return max(grid_px, math.ceil(grid_px * native_per_grid))

    def to_native_area(grid_px: int) -> int:
        return max(1, math.ceil(grid_px * native_per_grid * native_per_grid))

    raw_layers: list[tuple[int, str, tuple[int, int, int, int], np.ndarray]] = []
    cov = np.zeros((oh, ow), dtype=np.uint8)
    for idx, layer in enumerate(psd):
        im = layer.composite()
        if im is None:
            continue
        native_rect = map_bbox(layer.bbox, scale, pad_x, pad_y)
        try:
            arr, box = paste_onto_canvas(im, native_rect, ow, oh)
        except ValueError:
            continue
        dx0, dy0, dx1, dy1 = box
        cov[dy0:dy1, dx0:dx1] = np.maximum(cov[dy0:dy1, dx0:dx1], arr[:, :, 3])
        raw_layers.append((idx, layer.name, box, arr))

    if reproject:
        occ = np.zeros((oh, ow), dtype=bool)
        reprojected: list[tuple[int, str, tuple[int, int, int, int], np.ndarray]] = []
        for idx, lname, box, arr in reversed(raw_layers):
            out_arr = reproject_visible_pixels(
                arr,
                box,
                orig_arr,
                occ,
                dilate_iters=to_native_len(3),
                feather_sigma=1.5 * max(1.0, native_per_grid),
            )
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

    cov_bool = ndimage.binary_dilation(cov > 80, iterations=to_native_len(2))
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
        keep = ndimage.binary_dilation(keep, iterations=to_native_len(3)) & orig_bool
        ys, xs = np.where(keep)
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        crop = orig_arr[y0:y1, x0:x1].copy()
        crop[~keep[y0:y1, x0:x1]] = 0
        p = out_parts / out_name
        Image.fromarray(crop).save(p)
        rect = {"left": float(x0), "top": float(y0), "width": float(x1 - x0), "height": float(y1 - y0)}
        return p, (x1 - x0, y1 - y0), rect

    top_zone = np.zeros((oh, ow), dtype=bool)
    top_zone[: int(oh * top_zone_ratio), :] = True
    bot_zone = np.zeros((oh, ow), dtype=bool)
    bot_zone[int(oh * (1.0 - bottom_zone_ratio)) :, :] = True

    shoes_rec = extract_zone(bot_zone, "shoes_recovered.png", min_px=to_native_area(min_bottom_px))
    hat_rec = extract_zone(top_zone, "hat_recovered.png", min_px=to_native_area(min_top_px))

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
        "canvas": {"width": ow, "height": oh},
        "layers": layers,
    }
    (out_dir / "layout.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


# ---------- Web viewer upgrade & MOC3 validation ----------


def build_web_viewer_html(web_ui: Mapping[str, Any]) -> str:
    """Build export_web/index.html with independent ON/OFF prop/pose toggles."""
    title = html.escape(str(web_ui.get("title", "Live2D Viewer")))
    cfg_json = (
        json.dumps(dict(web_ui), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
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


def _as_dict(val: Any) -> dict[str, Any]:
    return val if isinstance(val, dict) else {}


def _as_list(val: Any) -> list[Any]:
    return val if isinstance(val, list) else []


def _extract_moc3_param_ids(raw: bytes) -> set[str] | None:
    """Extract 64-byte ASCII parameter IDs from a Cubism MOC3 binary when tables are present."""
    if len(raw) < 64 + 80 * 4:
        return None
    fmt = ">I" if raw[5] else "<I"
    count_off = struct.unpack_from(fmt, raw, 64)[0]
    p_off = struct.unpack_from(fmt, raw, 64 + 50 * 4)[0]
    if count_off + 24 > len(raw):
        return None
    n_params = struct.unpack_from(fmt, raw, count_off + 20)[0]
    if p_off == 0 or p_off + n_params * 64 > len(raw):
        return None
    return {
        raw[p_off + i * 64 : p_off + (i + 1) * 64].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        for i in range(n_params)
    }


def validate_moc3_dir(moc3_dir: Path, *, require_vtube_json: bool = False) -> dict[str, Any]:
    """Validate *.moc3 header, *.model3.json references, groups, and physics3 consistency."""
    issues: list[str] = []
    warnings: list[str] = []
    result: dict[str, Any] = {
        "dir": str(moc3_dir),
        "ok": False,
        "files": [],
        "moc3": None,
        "model3": None,
        "physics_unmatched_outputs": [],
        "issues": issues,
        "warnings": warnings,
    }
    if not moc3_dir.is_dir():
        result["error"] = "directory missing"
        issues.append("directory missing")
        return result

    root_resolved = moc3_dir.resolve()
    files = sorted(p.relative_to(moc3_dir).as_posix() for p in moc3_dir.rglob("*") if p.is_file())
    result["files"] = files

    moc_param_ids: set[str] | None = None
    moc_files = sorted(moc3_dir.rglob("*.moc3"))
    if moc_files:
        moc_path = moc_files[0]
        raw = moc_path.read_bytes()
        magic = raw[:4].decode("ascii", errors="replace")
        ver = raw[4] if len(raw) > 4 else None
        is_big_endian = raw[5] if len(raw) > 5 else None
        header_ok = magic == "MOC3" and ver in (1, 2, 3, 4, 5, 6) and len(raw) >= 64
        result["moc3"] = {
            "path": moc_path.relative_to(moc3_dir).as_posix(),
            "bytes": len(raw),
            "magic": magic,
            "version": ver,
            "big_endian": bool(is_big_endian),
            "header_ok": header_ok,
        }
        if not header_ok:
            issues.append(f"invalid .moc3 header or truncated file ({len(raw)} bytes)")
        else:
            moc_param_ids = _extract_moc3_param_ids(raw)
    else:
        issues.append("missing .moc3 file")

    m3_files = sorted(moc3_dir.rglob("*.model3.json"))
    if not m3_files:
        issues.append("missing .model3.json file")
    else:
        m3_path = m3_files[0]
        try:
            m3 = json.loads(m3_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            issues.append(f"invalid .model3.json: {exc}")
            m3 = None
        else:
            if not isinstance(m3, dict):
                issues.append("invalid .model3.json: expected JSON object")
                m3 = None

        if m3 is not None:
            refs = _as_dict(m3.get("FileReferences"))
            referenced: list[str] = []
            moc_ref = refs.get("Moc")
            if isinstance(moc_ref, str) and moc_ref:
                referenced.append(moc_ref)
            else:
                issues.append("model3.json missing FileReferences.Moc")
            textures = [t for t in _as_list(refs.get("Textures")) if isinstance(t, str)]
            referenced.extend(textures)
            for opt_key in ("Physics", "DisplayInfo"):
                opt_val = refs.get(opt_key)
                if isinstance(opt_val, str) and opt_val:
                    referenced.append(opt_val)
            expr_entries = [e for e in _as_list(refs.get("Expressions")) if isinstance(e, dict)]
            for ex in expr_entries:
                ex_file = ex.get("File")
                if isinstance(ex_file, str) and ex_file:
                    referenced.append(ex_file)
            motions_map = _as_dict(refs.get("Motions"))
            motions_counts: dict[str, int] = {}
            for mgroup, raw_mlist in motions_map.items():
                mlist = [mt for mt in _as_list(raw_mlist) if isinstance(mt, dict)]
                motions_counts[str(mgroup)] = len(mlist)
                for mt in mlist:
                    mt_file = mt.get("File")
                    if isinstance(mt_file, str) and mt_file:
                        referenced.append(mt_file)

            missing_files: list[str] = []
            for rel_ref in referenced:
                cand = (m3_path.parent / rel_ref).resolve()
                if not cand.is_relative_to(root_resolved) or not cand.is_file():
                    missing_files.append(rel_ref)
            if missing_files:
                issues.append(f"model3.json missing referenced files: {', '.join(missing_files)}")

            groups_map = {
                g["Name"]: _as_list(g.get("Ids"))
                for g in _as_list(m3.get("Groups"))
                if isinstance(g, dict) and isinstance(g.get("Name"), str) and g["Name"]
            }
            missing_groups = [g for g in REQUIRED_MODEL3_GROUPS if g not in groups_map]
            empty_groups = [g for g in REQUIRED_MODEL3_GROUPS if g in groups_map and not groups_map[g]]
            if missing_groups:
                warnings.append(f"model3.json missing groups: {', '.join(missing_groups)}")
            if empty_groups:
                warnings.append(f"model3.json empty groups: {', '.join(empty_groups)}")

            result["model3"] = {
                "path": m3_path.relative_to(moc3_dir).as_posix(),
                "version": m3.get("Version"),
                "moc": moc_ref if isinstance(moc_ref, str) else None,
                "textures": textures,
                "physics": refs.get("Physics") if isinstance(refs.get("Physics"), str) else None,
                "display_info": refs.get("DisplayInfo") if isinstance(refs.get("DisplayInfo"), str) else None,
                "expressions": [e.get("Name") for e in expr_entries],
                "motions": motions_counts,
                "groups": list(groups_map.keys()),
                "missing_files": missing_files,
                "missing_groups": missing_groups,
                "empty_groups": empty_groups,
            }

            phys_ref = refs.get("Physics")
            if isinstance(phys_ref, str) and phys_ref and phys_ref not in missing_files:
                phys_path = (m3_path.parent / phys_ref).resolve()
                try:
                    phys_data = json.loads(phys_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, ValueError) as exc:
                    issues.append(f"invalid physics3.json: {exc}")
                    phys_data = None
                else:
                    if not isinstance(phys_data, dict):
                        issues.append("invalid physics3.json: expected JSON object")
                        phys_data = None
                if phys_data is not None and moc_param_ids is not None:
                    unmatched: list[str] = []
                    for ps in _as_list(phys_data.get("PhysicsSettings")):
                        for out in _as_list(_as_dict(ps).get("Output")):
                            pid = _as_dict(_as_dict(out).get("Destination")).get("Id")
                            if not isinstance(pid, str):
                                issues.append("physics3.json output entry missing Destination.Id")
                            elif pid not in moc_param_ids:
                                unmatched.append(pid)
                    result["physics_unmatched_outputs"] = unmatched
                    if unmatched:
                        warnings.append(
                            f"physics3.json has {len(unmatched)} output parameter ID(s) not present in .moc3"
                        )

    if require_vtube_json:
        vts_files = [f for f in files if f.endswith(".vtube.json")]
        result["vtube_json"] = vts_files[0] if vts_files else None
        if not vts_files:
            issues.append("missing .vtube.json file")

    result["ok"] = len(issues) == 0
    return result


def summarize_export_losses(export_results: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize dropped and approximated compiler losses reported by PSD2Live exports."""
    total_dropped = 0
    total_approximated = 0
    by_target: dict[str, dict[str, Any]] = {}
    for target, info in export_results.items():
        if not info.get("ok"):
            continue
        res = info.get("result")
        losses = res.get("losses", []) if isinstance(res, Mapping) else []
        dropped_items = [item for item in losses if isinstance(item, Mapping) and item.get("handling") == "dropped"]
        approx_count = sum(
            1 for item in losses if isinstance(item, Mapping) and item.get("handling") == "approximated"
        )
        total_dropped += len(dropped_items)
        total_approximated += approx_count
        by_target[target] = {
            "dropped": len(dropped_items),
            "approximated": approx_count,
            "dropped_items": dropped_items,
        }
    return {
        "total_dropped": total_dropped,
        "total_approximated": total_approximated,
        "by_target": by_target,
    }


# ---------- PSD2Live Headless MCP Runner ----------


def discover_p2l_dir(explicit: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Resolve PSD2Live directory from --p2l-dir or PSD2LIVE_DIR (supports macOS .app bundles)."""
    env_map = os.environ if env is None else env
    raw = explicit if explicit is not None else (Path(env_map["PSD2LIVE_DIR"]) if env_map.get("PSD2LIVE_DIR") else None)
    if raw is None:
        raise FileNotFoundError(
            "PSD2Live directory not configured. Pass --p2l-dir <path> or set PSD2LIVE_DIR."
        )
    resolved = raw.resolve()
    if (resolved / "Contents" / "app").is_dir():
        return resolved / "Contents"
    if not (resolved / "app").is_dir():
        raise FileNotFoundError(f"PSD2Live app directory missing under {resolved}")
    return resolved


def find_mcp_proxy_dir(p2l_dir: Path) -> Path:
    """Locate the directory containing mcp_proxy.py across Windows, macOS, and Linux layouts."""
    candidates = (p2l_dir, p2l_dir / "app" / "resources", p2l_dir / "tools")
    for cand in candidates:
        if (cand / "mcp_proxy.py").is_file():
            return cand
    raise FileNotFoundError(
        f"mcp_proxy.py not found under {p2l_dir} (checked root, app/resources, tools)"
    )


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
    java_candidates = (
        p2l_dir / "runtime" / "bin" / "java.exe",
        p2l_dir / "runtime" / "bin" / "java",
        p2l_dir / "runtime" / "Contents" / "Home" / "bin" / "java",
    )
    java_exe = next((str(p) for p in java_candidates if p.exists()), "java")
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


class MCPExitedEarly(RuntimeError):
    """PSD2Live's MCP process exited before it answered the readiness probe."""


_STARTUP_ATTEMPTS_AUTO_PORT = 3


def _stop_process(proc: Any) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _install_exit_on_signals() -> dict[int, Any]:
    """Turn SIGTERM/SIGHUP into SystemExit so cleanup in ``finally`` blocks runs.

    Returns the previous handlers; empty when not on the main thread (Python only
    allows signal handlers there). SIGKILL cannot be caught.
    """
    if threading.current_thread() is not threading.main_thread():
        return {}

    def _exit(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, _exit)
    return previous


def _restore_signals(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def pick_ephemeral_port() -> int:
    """Return an unused localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def redact_token_in_file(path: Path, token: str) -> None:
    """Replace occurrences of bearer token in a log file with <REDACTED>."""
    if not token or not path.is_file():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if token in text:
        path.write_text(text.replace(token, "<REDACTED>"), encoding="utf-8")


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def wait_for_mcp(
    port: int,
    proc: Any,
    timeout: float = 30.0,
    *,
    token: str = "",
    opener: Callable[..., Any] = _NO_PROXY_OPENER,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.time,
) -> None:
    """Poll local PSD2Live MCP endpoint with an authenticated JSON-RPC probe."""
    deadline = time_fn() + timeout
    url = f"http://127.0.0.1:{port}/mcp"
    probe_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "live2d-pipeline-probe", "version": "1"},
            },
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    while time_fn() < deadline:
        if proc.poll() is not None:
            raise MCPExitedEarly(f"PSD2Live MCP exited early with code {proc.returncode}")
        req = urllib.request.Request(url, data=probe_body, headers=headers, method="POST")
        try:
            with opener(req, timeout=1.0) as resp:
                raw_body = resp.read()
            if b"jsonrpc" in raw_body:
                return
        except Exception:
            pass
        sleep_fn(0.4)
    raise TimeoutError(f"PSD2Live MCP did not start on port {port} within {timeout}s")


def _default_client_factory(p2l_dir: Path, endpoint: str, token: str) -> Any:
    proxy_dir = str(find_mcp_proxy_dir(p2l_dir))
    if proxy_dir not in sys.path:
        sys.path.insert(0, proxy_dir)
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

    canvas_warp_names = {cw["id"] for cw in recipe.get("canvas_warps", [])} | {
        cw.get("name", cw["id"]) for cw in recipe.get("canvas_warps", [])
    }

    def is_phase_b_axis(ax: Mapping[str, Any]) -> bool:
        if ax["parameter"]["id"] in clamped_pids:
            return True
        return any(
            sh["target"].startswith("deformer:") and sh["target"].split(":", 1)[1] in canvas_warp_names
            for sh in ax.get("shapes", [])
        )

    for ax in recipe.get("author_axes", []):
        if not is_phase_b_axis(ax):
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
        if is_phase_b_axis(ax):
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
        if pm_targets:
            p2l.edit("modifier_add", type="physics", targets=pm_targets, config=pm_cfg)
        else:
            pm_label = pm.get("id") or pm.get("name") or "?"
            warnings.append(f"physics_modifier '{pm_label}' skipped: no valid layer_ids resolved")

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
    port: int = 0,
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

    for stale in [*out_resolved.glob("poses_face*.png"), *out_resolved.glob("poses_sabun*.png")]:
        stale.unlink()

    p2l_resolved = discover_p2l_dir(p2l_dir)
    token = f"p2l_{secrets.token_hex(16)}"
    attempts = 1 if port > 0 else _STARTUP_ATTEMPTS_AUTO_PORT
    popen_kwargs: dict[str, Any] = {"text": True}
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if no_window:
        popen_kwargs["creationflags"] = no_window

    stdout_log = out_resolved / "_mcp_stdout.log"
    stderr_log = out_resolved / "_mcp_stderr.log"
    factory = client_factory or _default_client_factory
    t0 = time.perf_counter()
    previous_handlers = _install_exit_on_signals()
    try:
        with (
            open(stdout_log, "w", encoding="utf-8") as log_out,
            open(stderr_log, "w", encoding="utf-8") as log_err,
        ):
            proc = None
            try:
                attempt = 1
                while True:
                    # An auto-picked port can be taken before Java binds it: retry on a new one.
                    actual_port = port if port > 0 else pick_ephemeral_port()
                    cmd = build_p2l_mcp_cmd(p2l_resolved, actual_port, token, store_dir)
                    proc = popen_fn(cmd, stdout=log_out, stderr=log_err, **popen_kwargs)
                    try:
                        wait_fn(actual_port, proc, token=token)
                        break
                    except MCPExitedEarly:
                        if attempt >= attempts:
                            raise
                        _stop_process(proc)
                        proc = None
                        attempt += 1
                endpoint = f"http://127.0.0.1:{actual_port}/mcp"
                with factory(p2l_resolved, endpoint, token) as p2l:
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
                        face_poses = face_poses + extra

                    face_chunk_size = 9
                    for idx in range(0, len(face_poses), face_chunk_size):
                        chunk = face_poses[idx : idx + face_chunk_size]
                        _, fimgs = p2l.call(
                            "view_render_poses",
                            {
                                "viewport": {"mode": "canvas_rect", "left": fl, "top": ft, "width": fw, "height": fh},
                                "poses": chunk,
                                "columns": 3,
                                "background": "checkerboard",
                                "target_long_edge": 1600,
                            },
                        )
                        if fimgs:
                            part_i = idx // face_chunk_size + 1
                            fname = "poses_face.png" if len(face_poses) <= face_chunk_size else f"poses_face_{part_i}.png"
                            (out_resolved / fname).write_bytes(fimgs[0])

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
                    target_names = [t for t, _ in targets]
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

                    moc_check = validate_moc3_dir(out_resolved / "export_moc3") if "moc3" in target_names else None
                    vts_check = (
                        validate_moc3_dir(out_resolved / "export_vtube-studio", require_vtube_json=True)
                        if "vtube-studio" in target_names
                        else None
                    )
                    loss_summary = summarize_export_losses(export_results)
                    report = {
                        "name": name,
                        "elapsed_total_s": round(time.perf_counter() - t0, 2),
                        "post_rig_summary": post_summary,
                        "exports": export_results,
                        "export_losses": loss_summary,
                        "moc3_validation": moc_check,
                        "vts_validation": vts_check,
                    }
                    (out_resolved / "export_report.json").write_text(
                        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    return report
            finally:
                if proc is not None:
                    _stop_process(proc)
    finally:
        redact_token_in_file(stdout_log, token)
        redact_token_in_file(stderr_log, token)
        _restore_signals(previous_handlers)


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
    p_run.add_argument("--port", type=int, default=0, help="Local TCP port (0 = auto ephemeral port)")
    p_run.add_argument("--face-rect", default="")
    p_run.add_argument("--expressions", type=Path, default=None)
    p_run.add_argument("--extra-poses", type=Path, default=None)
    p_run.add_argument("--post-rig", type=Path, default=None)
    p_run.add_argument("--sabun-poses", type=Path, default=None)
    p_run.add_argument(
        "--allow-export-losses",
        action="store_true",
        help="Allow exit 0 even when PSD2Live reports handling=='dropped' export losses",
    )

    p_web = sub.add_parser("patch-web", help="Upgrade export_web/index.html with independent toggles")
    p_web.add_argument("index_html", type=Path)
    p_web.add_argument("--config", type=Path, required=True)

    p_val = sub.add_parser("validate-moc3", help="Validate exported .moc3 header and .model3.json")
    p_val.add_argument("moc3_dir", type=Path)
    p_val.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
                raise ValueError("--rect must be left,top,width,height")
            rect = {"left": nums[0], "top": nums[1], "width": nums[2], "height": nums[3]}
            warns: list[str] = []
            res = split_eyes_and_brows(args.src_png, args.out, args.prefix, rect, warnings=warns)
            for msg in warns:
                print(f"WARNING: {msg}", file=sys.stderr)
            payload: dict[str, Any] = {
                k: {"path": str(v[0]), "size": v[1], "rect": v[2]} for k, v in res.items()
            }
            payload["warnings"] = warns
            print(json.dumps(payload, ensure_ascii=False))
            return 0

        if args.cmd == "patch-web":
            cfg = json.loads(args.config.read_text(encoding="utf-8"))
            patch_web_viewer(args.index_html, cfg)
            print(f"Patched {args.index_html}")
            return 0

        if args.cmd == "validate-moc3":
            res = validate_moc3_dir(args.moc3_dir)
            print(json.dumps(res, indent=2 if not args.json else None, ensure_ascii=False))
            return 0 if res.get("ok") else 1

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
        moc_val = report.get("moc3_validation")
        vts_val = report.get("vts_validation")
        moc_ok = moc_val is None or bool(moc_val.get("ok"))
        vts_ok = vts_val is None or bool(vts_val.get("ok"))
        losses_ok = args.allow_export_losses or report.get("export_losses", {}).get("total_dropped", 0) == 0
        return 0 if (all_exports_ok and moc_ok and vts_ok and losses_ok) else 1
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
