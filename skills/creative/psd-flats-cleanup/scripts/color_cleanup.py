#!/usr/bin/env python3
"""Polish flats in a layered RGB PSD, preserving the source document.

Originally written by staryume (https://github.com/staryume) as Coloring Helper,
https://github.com/staryume/ColoringHelper at ed6315b, and included here under this
repository's MIT license with the author's agreement. Added for agents: the `layers` and
`settings` commands, setup flags instead of prompts and hand edits, a canvas size limit,
copying where a volume has no hard links, and matching flats by group path in `compare`.

  uv run --no-project --no-config --no-build --python 3.12 --with 'psd-tools[composite]==1.27.0' \\
    --with numpy==2.5.5 --with pillow==13.0.0 --with scipy==1.18.1 --with attrs==26.1.0 \\
    --with aggdraw==1.4.1 python -I color_cleanup.py layers Artwork.psd
"""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import math
import os
import re
import shutil
import sys
import unicodedata

import numpy as np
from PIL import Image, ImageDraw
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer
from psd_tools.constants import BlendMode, ColorMode, Compression, Resource
from scipy import ndimage as ndi
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


# Peak memory measured on a 4160x3200 canvas with one flat: 4.2 GB, about 320 bytes per pixel.
BYTES_PER_PIXEL = 320
MAX_MEGAPIXELS = 40
SETTINGS = ("layers", "protect_boxes", "max_region_px", "ignore")


def read_psd(path):
    psd = PSDImage.open(path)
    if psd.color_mode != ColorMode.RGB or psd.depth != 8:
        raise ValueError("Use an 8-bit RGB PSD with separate raster flats and lineart.")
    return psd


def shown(text):
    """Escape characters a terminal would act on (newlines, escapes, bidi controls) in a name
    from the file. Spaces of any width, as in Japanese names, stay as they are."""
    return "".join(c if c.isprintable() or unicodedata.category(c) == "Zs"
                   else c.encode("unicode_escape").decode("ascii") for c in text)


def path_text(names):
    return shown(" / ".join(names))


def layer_path(layer):
    """The group and layer names from the document root down to `layer`."""
    names = []
    while layer.parent is not None:
        names.append(layer.name)
        layer = layer.parent
    return tuple(reversed(names))


def publish(staged, path):
    """Put a finished file at `path` without replacing anything there.

    A hard link is atomic. Volumes without hard links (exFAT, FAT32, some network shares) get
    an exclusive copy instead; both refuse an existing file or symlink.
    """
    try:
        os.link(staged, path)
        return
    except FileExistsError:
        raise
    except OSError:
        pass
    with open(staged, "rb") as source, open(path, "xb") as target:
        try:
            shutil.copyfileobj(source, target)
        except BaseException:
            target.close()
            path.unlink()
            raise


def raster_layers(group, indices=(), names=()):
    """Yield raster leaves in PSD stacking order, from bottom to top."""
    for index, layer in enumerate(group):
        ref = {"index": [*indices, index], "names": [*names, layer.name]}
        if layer.is_group():
            yield from raster_layers(layer, ref["index"], ref["names"])
        elif layer.kind == "pixel":
            yield ref, layer


def pixels(layer):
    image = layer.topil(apply_icc=False)
    if image is None:
        raise ValueError(f"Layer {layer.name!r} has no raster data.")
    return np.array(image.convert("RGBA"))


def window(layer, shape):
    """Return aligned canvas and layer slices, including partially off-canvas layers."""
    height, width = shape
    left, top, right, bottom = layer.bbox
    x0, y0, x1, y1 = max(0, left), max(0, top), min(width, right), min(height, bottom)
    if x0 >= x1 or y0 >= y1:
        raise ValueError(f"Layer {layer.name!r} has no pixels inside the canvas.")
    return (slice(y0, y1), slice(x0, x1)), (slice(y0-top, y1-top), slice(x0-left, x1-left))


def suggested_role(layer, ref, shape):
    if not layer.is_visible() or not layer.width or not layer.height:
        return "skip"
    title = "/".join(ref["names"]).lower()
    if re.search(r"eye.?white|protect|highlight|sclera|iris", title):
        return "protect"
    if re.search(r"line\s*art|(?:^|/)line\d*(?:/|$)|(?:^|[ /_-])ink(?:s|ing|ed)?(?:$|[ /_\d-])"
                 r"|(?:^|[ /_])lines?(?:$|[ /_])", title):
        return "lineart"
    if re.search(r"background|(?:^|[ /_])(?:bg|paper)(?:$|[ /_])", title):
        return "background"
    if layer.blend_mode == BlendMode.MULTIPLY:
        return "lineart"
    if re.search(r"flat|base.?colou?r", title):
        return "flat"
    sample = pixels(layer)[::8, ::8]
    solid = sample[..., 3] >= 240
    if sample[..., 3].min() >= 240 and layer.size == shape[::-1]:
        return "background"
    if solid.any() and layer.blend_mode == BlendMode.NORMAL:
        colors, counts = np.unique(sample[solid, :3], axis=0, return_counts=True)
        if counts.max() >= 0.6 * counts.sum():
            if solid.mean() >= 0.10 or colors[counts.argmax()].mean() >= 110:
                return "flat"
            return "lineart"
    if (sample[..., 3] > 0).mean() < 0.15:
        return "lineart"
    return "skip"


def config_path(source):
    return source.with_suffix(".cleanup.json")


ROLES = ("lineart", "flat", "background", "protect")


def layer_rows(psd):
    """Return (reference, suggested role) for each visible raster layer, bottom to top."""
    shape = (psd.height, psd.width)
    return [(ref, suggested_role(layer, ref, shape)) for ref, layer in raster_layers(psd)
            if layer.is_visible() and layer.width and layer.height]


def canvas_size(size):
    width, height = size
    return {"width": width, "height": height, "megapixels": round(width * height / 1e6, 3),
            "memory_gb": round(width * height * BYTES_PER_PIXEL / 1e9, 1)}


def print_layers(rows, size, as_json=False):
    canvas = canvas_size(size)
    if as_json:
        print(json.dumps({"canvas": canvas, "layers": [{"number": number, "suggested": role, **ref}
                          for number, (ref, role) in enumerate(rows, 1)]}, indent=2))
        return
    print(f"Canvas {canvas['width']}×{canvas['height']} ({canvas['megapixels']:.1f} MP); polish needs about "
          f"{canvas['memory_gb']:.1f} GB of memory with one flat, more with many.")
    print("Visible raster layers, bottom to top")
    for number, (ref, role) in enumerate(rows, 1):
        print(f"{number:>3}  {role:<10}  {path_text(ref['names'])}")


def print_settings(config, region):
    for role in (*ROLES, "ignore"):
        refs = config.get("ignore", []) if role == "ignore" else config["layers"].get(role, [])
        print(f"{role:<10}  {', '.join(path_text(ref['names']) for ref in refs) or '-'}")
    print(f"protect_boxes {' '.join(map(str, config.get('protect_boxes', []))) or '-'}")
    print(f"max_region_px {region}")


def check_ref(ref, where):
    if not (isinstance(ref, dict) and set(ref) == {"index", "names"}
            and isinstance(ref["index"], list) and isinstance(ref["names"], list)
            and ref["index"] and len(ref["index"]) == len(ref["names"])
            and all(type(i) is int for i in ref["index"]) and all(isinstance(n, str) for n in ref["names"])):
        raise ValueError(f"{where}: each layer must be {{\"index\": [numbers], \"names\": [text]}} as setup wrote it.")
    return tuple(ref["index"])


def check_box(box, shape=None):
    if not (isinstance(box, list) and len(box) == 4 and all(type(v) is int for v in box)):
        raise ValueError("Each protect_boxes entry must be [left, top, right, bottom] in whole pixels.")
    x0, y0, x1, y1 = box
    if shape is not None and not (0 <= x0 < x1 <= shape[1] and 0 <= y0 < y1 <= shape[0]):
        raise ValueError("Each protect_boxes entry must be [left, top, right, bottom] within the canvas.")
    return box


def check_settings(config):
    """Refuse a settings file that setup could not have written, before any of it is used."""
    if not isinstance(config, dict) or not isinstance(config.get("layers"), dict):
        raise ValueError("The settings file must hold a \"layers\" object. Move it aside and run setup again.")
    for key in config:
        if key not in SETTINGS:
            raise ValueError(f"Unknown setting {shown(str(key))!r}. Move the file aside and run setup again.")
    for role, refs in config["layers"].items():
        if role not in ROLES:
            raise ValueError(f"Unknown role {shown(str(role))!r} in the settings file.")
        if not isinstance(refs, list):
            raise ValueError(f"{role}: expected a list of layers.")
        for ref in refs:
            check_ref(ref, role)
    boxes = config.get("protect_boxes", [])
    for box in boxes if isinstance(boxes, list) else [None]:
        check_box(box)
    ignore = config.get("ignore", [])
    if not isinstance(ignore, list):
        raise ValueError("ignore: expected a list of layers.")
    for ref in ignore:
        check_ref(ref, "ignore")


def region_px(config, shape):
    island = config.get("max_region_px", max(8, min(64, round(min(shape)/100))))
    if type(island) is not int or not 8 <= island <= 128:
        raise ValueError("max_region_px must be an integer from 8 to 128.")
    return island


def parse_box(text):
    try:
        box = [int(v) for v in text.split(",")]
    except ValueError:
        box = None
    return check_box(box)


def parse_numbers(answer, defaults, role, count):
    """Turn '3,5', '' (keep the suggestion) or '-' (none) into checked layer numbers."""
    try:
        ids = [] if answer == "-" else [int(i) for i in (answer.split(",") if answer else defaults)]
    except ValueError:
        ids = None
    if ids is None or len(ids) != len(set(ids)) or any(i < 1 or i > count for i in ids) \
            or (role in ("lineart", "flat") and not ids):
        raise ValueError(f"{role}: enter comma-separated layer numbers from the table"
                         f"{'' if role in ('background', 'protect') else ' (at least one)'}.")
    return ids


def setup(source, choices=None, ignore=None, max_region_px=None, boxes=()):
    """Save the layer roles and settings. `choices` maps a role to '3,5', '-' or None (the
    suggestion); without it, ask on the terminal. `ignore` takes layer numbers like a role."""
    psd = read_psd(source)
    if config_path(source).exists():
        raise FileExistsError(f"{config_path(source)} already exists. Move it aside to set up again.")
    rows = layer_rows(psd)
    shape = (psd.height, psd.width)
    if choices is None:
        print_layers(rows, psd.size)
    roles = {}
    for role in ROLES:
        defaults = [str(i) for i, (_, guess) in enumerate(rows, 1) if guess == role]
        while True:
            if choices is None:
                answer = input(f"{role} layer numbers [{','.join(defaults) or 'none'}] "
                               "(Enter accepts, - clears): ").strip()
            else:
                answer = (choices.get(role) or "").strip()
            try:
                roles[role] = [rows[i-1][0] for i in parse_numbers(answer, defaults, role, len(rows))]
                break
            except ValueError as error:
                if choices is not None:
                    raise
                print(error)
    config = {"layers": roles, "protect_boxes": [check_box(box, shape) for box in boxes]}
    if ignore:
        config["ignore"] = [rows[i-1][0] for i in parse_numbers(ignore, [], "ignore", len(rows))]
        check_ignore(config)
    if max_region_px is not None:
        config["max_region_px"] = max_region_px
    region = region_px(config, shape)
    resolve_config(psd, config)
    with config_path(source).open("x", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print_settings(config, region)
    print(f"Saved {config_path(source)}")


def check_ignore(config):
    roles = {tuple(ref["index"]) for refs in config["layers"].values() for ref in refs}
    for ref in config.get("ignore", []):
        if tuple(ref["index"]) in roles:
            raise ValueError(f"An ignored layer cannot also have a role: {path_text(ref['names'])}.")


def resolve_config(psd, config):
    check_settings(config)
    available = {tuple(ref["index"]): (ref, layer) for ref, layer in raster_layers(psd)}
    used = set()
    resolved = {}
    for role in ROLES:
        selections = []
        for ref in config["layers"].get(role, []):
            path = tuple(ref["index"])
            found = available.get(path)
            if found is None or found[0]["names"] != ref["names"] or not found[1].is_visible():
                name = path_text(ref["names"])
                raise ValueError(f"Layer moved, changed name, or became hidden: {name}. "
                                 "Move the old settings aside and rerun setup.")
            if path in used:
                raise ValueError("Assign each layer to exactly one role.")
            used.add(path)
            selections.append((path, found[1]))
        resolved[role] = [layer for _, layer in sorted(selections)]
    if not resolved["lineart"] or not resolved["flat"]:
        raise ValueError("Choose at least one visible lineart layer and one visible flat layer.")
    for layer in resolved["flat"]:
        if layer.blend_mode != BlendMode.NORMAL or layer.opacity != 255 or layer.fill_opacity != 255:
            raise ValueError(f"Choose fully opaque Normal flat layers: {layer.name!r}.")
        node = layer
        while node.parent is not None:
            if node.has_mask() or node.has_vector_mask() or node.clipping or node.has_effects():
                raise ValueError(f"Use plain flats and groups without masks, clipping, or effects: {node.name!r}.")
            if node.opacity != 255 or node.blend_mode not in (BlendMode.NORMAL, BlendMode.PASS_THROUGH):
                raise ValueError(f"Use Normal or Pass Through flat groups at 100% opacity: {node.name!r}.")
            node = node.parent
    lowest_flat = min(tuple(ref["index"]) for ref in config["layers"]["flat"])
    if any(tuple(ref["index"]) > lowest_flat for ref in config["layers"].get("background", [])):
        raise ValueError("Background layers must be below the editable flats.")
    return resolved


def read_paint(shape, flats, protected, boxes):
    """Return the topmost flat at each pixel (0 = none), where it is opaque, and the protected area."""
    top = np.zeros(shape, dtype=np.int16)
    solid = np.ones(shape, dtype=bool)
    shield = np.zeros(shape, dtype=bool)
    for index, layer in enumerate(flats, 1):
        canvas, local = window(layer, shape)
        alpha = pixels(layer)[local][..., 3]
        if not (alpha >= 240).any():
            raise ValueError(f"Flat {layer.name!r} has no solid color to use as a seed.")
        occupied = alpha >= 24
        top[canvas][occupied] = index
        solid[canvas][occupied] = alpha[occupied] >= 240
    for layer in protected:
        canvas, local = window(layer, shape)
        shield[canvas] |= pixels(layer)[local][..., 3] >= 8
    for box in boxes:
        x0, y0, x1, y1 = check_box(box, shape)
        # A box keeps bare canvas bare; paint inside it can still be corrected.
        shield[y0:y1, x0:x1] |= top[y0:y1, x0:x1] == 0
    return top, solid, ndi.maximum_filter(shield, size=3)


def read_ink(shape, lineart):
    ink = np.zeros(shape, dtype=np.uint8)
    for layer in lineart:
        canvas, local = window(layer, shape)
        data = pixels(layer)[local].astype(np.float32)
        channel_min = data[..., :3].min(axis=2)
        alpha = data[..., 3] * (layer.opacity / 255)
        # On transparent ink layers use alpha as evidence even for light-colored ink.
        # On opaque Multiply scans, white paper contributes no contour.
        strength = alpha * (1 - channel_min / 255)
        if layer.blend_mode != BlendMode.MULTIPLY and np.any(data[..., 3] == 0):
            strength = np.maximum(strength, alpha * 0.5)
        np.maximum(ink[canvas], strength.astype(np.uint8), out=ink[canvas])
    return ink


EIGHT = np.ones((3, 3), bool)
CROSS = ndi.generate_binary_structure(2, 1)
SIDES = ((np.s_[:, :-1], np.s_[:, 1:]), (np.s_[:, 1:], np.s_[:, :-1]),
         (np.s_[:-1, :], np.s_[1:, :]), (np.s_[1:, :], np.s_[:-1, :]))


def paint_regions(top, free, kinds, neck):
    """Label connected areas of one paint between lines.

    A ball of radius `neck` rolls inside each paint: what it reaches from one spot is a
    region, and it cannot pass a break in a line. Every thinner leftover is a sliver of
    its own. Returns the label image plus each region's paint and half-width.
    """
    regions = np.zeros(top.shape, np.int32)
    kind, half = [0], [0.0]
    for paint in range(kinds):
        mask = free & (top == paint)
        if not mask.any():
            continue
        box = ndi.find_objects(mask.astype(np.uint8))[0]
        inside = mask[box]
        depth = ndi.distance_transform_edt(np.pad(inside, 1))[1:-1, 1:-1]
        cores, count = ndi.label(depth > neck, EIGHT)
        view = regions[box]
        if count:
            away, (iy, ix) = ndi.distance_transform_edt(cores == 0, return_indices=True)
            reached = inside & (away <= neck)
            view[reached] = cores[iy[reached], ix[reached]] + len(kind) - 1
            half += list(ndi.maximum(depth, cores, np.arange(1, count + 1)))
            kind += [paint] * count
        slivers, count = ndi.label(inside & (view == 0), CROSS)
        if count:
            view[slivers > 0] = slivers[slivers > 0] + len(kind) - 1
            half += list(ndi.maximum(depth, slivers, np.arange(1, count + 1)))
            kind += [paint] * count
    return regions, np.array(kind), np.array(half)


def borders(regions):
    """Return (region, neighbour, shared edge length) for every pair of touching regions."""
    count = int(regions.max()) + 1
    keys = []
    for here, there in SIDES:
        a, b = regions[here], regions[there]
        touching = (a > 0) & (b > 0) & (a != b)
        keys.append(a[touching].astype(np.int64) * count + b[touching])
    keys, length = np.unique(np.concatenate(keys), return_counts=True)
    return keys // count, keys % count, length


def join_pieces(regions, kind, half, thin):
    """Rejoin thin pieces of one paint that meet along a real opening, so a tip is judged whole.

    Only thin pieces join. A strand joined to the spill beside it would follow the spill.
    """
    a, b, length = borders(regions)
    real = length >= np.maximum(4, 1.5 * np.minimum(half[a], half[b]))
    join = (kind[a] == kind[b]) & (half[a] < thin) & (half[b] < thin) & real
    n = len(kind)
    count, group = connected_components(coo_matrix((np.ones(join.sum()), (a[join], b[join])), shape=(n, n)), directed=False)
    widest = np.zeros(count)
    np.maximum.at(widest, group, half)
    paint = np.zeros(count, kind.dtype)
    paint[group] = kind
    return group[regions].astype(np.int32), paint, widest


def correction_map(top, solid, shield, ink, kinds, island):
    """Return the flat each pixel should move to (-1 = leave alone) and the filled pockets.

    The flats are rough block-ins. A paint edge is trusted only on a line, or where two wide
    areas meet. A region narrower than `island` that lines partly wall in joins the wider
    region it opens into; a small unpainted region sealed by lines takes the nearest paint.
    Whole regions move, so no new edge appears away from a line.
    """
    neck = max(1, island // 7)
    stroke = ink >= 55
    regions, kind, half = join_pieces(*paint_regions(top, ~stroke, kinds, neck), 2 * neck)
    n = len(kind)
    if n == 1:
        return np.full(top.shape, -1, np.int16), np.zeros(top.shape, bool)
    area = np.bincount(regions.ravel(), minlength=n)
    a, b, length = borders(regions)
    # The canvas edge is an open side, like a neighbouring region.
    rim = sum(np.bincount(regions[side], minlength=n) for side in (np.s_[0, :], np.s_[-1, :], np.s_[:, 0], np.s_[:, -1]))
    touch = np.bincount(a, length, minlength=n) + rim
    wall = np.zeros(n)
    for here, there in SIDES:
        wall += np.bincount(regions[here][stroke[there]], minlength=n)
    # A shape painted without lines is deliberate. Only a region that lines partly wall in,
    # or a pinhole, can be a slip of the brush.
    small = (half < island) & ((wall >= 0.25 * (wall + touch)) | ((kind == 0) & (area <= island * island / 8)))
    small[0] = False
    result = kind.copy()
    for _ in range(3):
        # A thin piece counts as wide as the paint it opens into: the tip of a strand belongs
        # to the strand. A break in a line is not an opening.
        family = half.copy()
        joined = result[a] == result[b]
        opening = joined & (length >= np.maximum(4, 1.5 * np.minimum(half[a], half[b])))
        for _ in range(4):
            np.maximum.at(family, a[opening], family[b[opening]])
        # Only a wider neighbour, or more of the same paint, can claim a region. Where two
        # regions could claim each other, the one that is thinner in itself gives way.
        claim = small[a] & (((family[b] > 1.2 * half[a]) & (half[b] >= half[a])) | joined)
        near = np.bincount(a[claim] * kinds + result[b[claim]], length[claim], minlength=n * kinds).reshape(n, kinds)
        best, reach = near.argmax(1), near.max(1)
        move = small & (best != result) & (reach >= np.maximum(1.5 * half, 4)) & (reach > 2 * near[np.arange(n), result])
        # Paint that fills a channel from line to line was put there on purpose: it can
        # change color, but it is not erased.
        move &= ~((best == 0) & (wall >= 0.85 * (wall + touch)))
        if not move.any():
            break
        result[move] = best[move]
    # Unpainted and walled in by ink almost all the way round: a pocket or an unpainted strand.
    blank = (result == 0) & (kind == 0) & ((half <= 0.3 * island) | (area <= 2.5 * island * island))
    blank[0] = False
    pocket = np.zeros(n, bool)
    for _ in range(3):
        outside = ~pocket[b]
        open_edge = np.bincount(a[outside], length[outside], minlength=n) + rim
        # The narrow end of a wider unpainted gap is part of that gap, not a pocket.
        gap = outside & (result[b] == 0)
        same = np.bincount(a[gap], length[gap], minlength=n) + rim
        pocket = (blank & (same < np.maximum(1.5 * half, 4))
                  & ((open_edge < np.maximum(1.5 * half, 4)) | (wall >= 0.85 * (wall + open_edge))))
    want = result[regions]
    if pocket.any() and (want > 0).any():
        iy, ix = ndi.distance_transform_edt((want == 0) | stroke, return_indices=True)[1]
        inside = pocket[regions]
        votes = np.bincount(regions[inside].astype(np.int64) * kinds + want[iy[inside], ix[inside]],
                            minlength=n * kinds).reshape(n, kinds)
        result[pocket] = votes[pocket].argmax(1)
        want = result[regions]
    # Ink follows the nearest region, so paint meets at the middle of each line.
    iy, ix = ndi.distance_transform_edt(regions == 0, return_indices=True)[1]
    want = want[iy, ix]
    # Soft paint is made solid only beside a line, where it would let the paper show through.
    beside = ndi.maximum_filter(stroke, size=2 * neck + 1)
    fix = ((want != top) | ((want > 0) & ~solid & beside)) & ~shield
    return np.where(fix, want, -1).astype(np.int16), pocket[regions] & ~shield


def apply_corrections(psd, flats, target):
    changed = 0
    for index, original in enumerate(flats, 1):
        canvas, local = window(original, target.shape)
        data = pixels(original)
        trim_inside = (target[canvas] >= 0) & (target[canvas] < index) & (data[local][..., 3] > 0)
        paint_y, paint_x = np.nonzero(target == index)
        if not trim_inside.any() and not paint_y.size:
            continue
        left, top, right, bottom = original.bbox
        if paint_y.size:
            x0, y0 = min(left, int(paint_x.min())), min(top, int(paint_y.min()))
            x1, y1 = max(right, int(paint_x.max())+1), max(bottom, int(paint_y.max())+1)
            larger = np.zeros((y1-y0, x1-x0, 4), dtype=np.uint8)
            larger[top-y0:bottom-y0, left-x0:right-x0] = data
            # New paint copies the nearest solid color already on this layer.
            iy, ix = ndi.distance_transform_edt(larger[..., 3] < 240, return_indices=True)[1]
            paint = (paint_y-y0, paint_x-x0)
            source = larger[iy[paint], ix[paint]]
            larger[top-y0:bottom-y0, left-x0:right-x0][local][trim_inside] = 0
            larger[paint] = source
            larger[paint + (3,)] = 255
            data, left, top = larger, x0, y0
        else:
            data[local][trim_inside] = 0
        parent = original.parent
        corrected = PixelLayer.fromarray(data, parent, name=f"{original.name} (polished)",
                                         top=top, left=left, compression=Compression.RLE)
        corrected_pos = parent.index(original) + 1
        parent.insert(corrected_pos, corrected)
        original.visible = False
        changed += 1
    return changed


def preview_sheet(before, after, corrections, uncertain, path):
    w, h = before.size
    marked = np.array(after.convert("RGB"))
    fixes = ndi.maximum_filter(corrections >= 0, size=5)
    flags = ndi.maximum_filter(uncertain, size=5)
    for mask, color in ((fixes & ~flags, (35, 245, 195)), (flags, (255, 155, 30))):
        marked[mask] = (0.25 * marked[mask] + 0.75 * np.array(color)).astype(np.uint8)
    header = max(24, w // 72)
    sheet = Image.new("RGB", (3*w, h+header), "#25252a")
    draw = ImageDraw.Draw(sheet)
    for panel, (title, image) in enumerate((
        ("Before", before), ("After", after),
        ("Review   /   Mint = edited   /   Amber = filled pocket, inspect", Image.fromarray(marked))
    )):
        draw.text((panel*w+8, 3), title, fill="#ffffff", font_size=max(14, header*2//3))
        sheet.paste(image, (panel*w, header))
    sheet.save(path)


def load(source, max_megapixels=MAX_MEGAPIXELS):
    """Open the artwork and work out its corrections from the saved settings."""
    psd = read_psd(source)
    canvas = canvas_size(psd.size)
    if canvas["megapixels"] > max_megapixels:
        raise ValueError(f"The canvas is {canvas['width']}×{canvas['height']} ({canvas['megapixels']:.1f} MP) and needs "
                         f"about {canvas['memory_gb']:.0f} GB of memory or more. If this machine has it, run again with "
                         f"--max-megapixels {math.ceil(canvas['megapixels'])}.")
    config = json.loads(config_path(source).read_text(encoding="utf-8"))
    layers = resolve_config(psd, config)
    available = {tuple(ref["index"]): (ref, layer) for ref, layer in raster_layers(psd)}
    for ref in config.get("ignore", []):
        found = available.get(tuple(ref["index"]))
        if found is None or found[0]["names"] != ref["names"]:
            raise ValueError(f"Ignored layer moved or changed name: {path_text(ref['names'])}.")
    check_ignore(config)
    for ref in config.get("ignore", []):
        available[tuple(ref["index"])][1].visible = False
    shape = (psd.height, psd.width)
    island = region_px(config, shape)
    print(f"Settings from {config_path(source).name}")
    print_settings(config, island)
    top, solid, shield = read_paint(shape, layers["flat"], layers["protect"], config.get("protect_boxes", []))
    ink = read_ink(shape, layers["lineart"])
    print("Matching paint to the lines…", flush=True)
    corrections, pockets = correction_map(top, solid, shield, ink, len(layers["flat"]) + 1, island)
    return psd, layers, top, ink, corrections, pockets


def show_settings(source):
    """Print the settings polish would apply, checked, with names escaped."""
    psd = read_psd(source)
    config = json.loads(config_path(source).read_text(encoding="utf-8"))
    resolve_config(psd, config)
    check_ignore(config)
    print(f"Settings from {config_path(source).name}")
    print_settings(config, region_px(config, (psd.height, psd.width)))


def compare(source, reference, output=None, max_megapixels=MAX_MEGAPIXELS):
    """Score the tool against a copy of the artwork that the artist finished by hand."""
    output = output or source.with_name(f"{source.stem}-compare.png")
    if output.suffix.lower() != ".png":
        raise ValueError("The output filename must end in .png.")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"{output} already exists. Choose another --output name.")
    print("Reading flats and lineart…", flush=True)
    psd, layers, top, ink, corrections, _ = load(source, max_megapixels)
    finished = read_psd(reference)
    if finished.size != psd.size:
        raise ValueError("The finished PSD must have the same canvas size as the source.")
    # Match by the whole group path: two flats called "Hair" in different groups are different.
    by_path = {}
    for ref, layer in raster_layers(finished):
        by_path.setdefault(tuple(ref["names"]), []).append(layer)

    def find(path):
        found = by_path.get(path, [])
        if len(found) > 1:
            raise ValueError(f"Two layers in the finished PSD share the path {path_text(path)}. "
                             "Rename one in both files.")
        return found[0] if found else None
    truth = np.zeros(top.shape, np.int16)
    dropped = []
    colors = np.full((len(layers["flat"]) + 1, 3), 255, np.uint8)
    for index, flat in enumerate(layers["flat"], 1):
        canvas, local = window(flat, top.shape)
        data = pixels(flat)[local]
        colors[index] = np.median(data[data[..., 3] >= 240, :3], axis=0)
        path = layer_path(flat)
        layer = find((*path[:-1], f"{path[-1]} (polished)"))
        layer = find(path) if layer is None else layer
        if layer is None or not layer.is_visible() or not layer.width:
            dropped.append(index)  # Hidden or removed in the finished file: not scored.
            continue
        canvas, local = window(layer, top.shape)
        truth[canvas][pixels(layer)[local][..., 3] >= 128] = index
    tool = np.where(corrections >= 0, corrections, top)
    free = (ink < 55) & ~np.isin(tool, dropped) & ~np.isin(top, dropped)
    asked = free & (top != truth)
    kept = free & (top == truth)
    wrong = free & (tool != truth)
    print(f"The artist changed {np.count_nonzero(asked):,} pixels between the lines. "
          f"The tool matches {100 * np.count_nonzero(asked & ~wrong) / max(1, np.count_nonzero(asked)):.1f}% of them.")
    print(f"The tool changed {np.count_nonzero(kept & wrong):,} pixels that the artist left alone. "
          f"{np.count_nonzero(wrong):,} pixels still differ.")
    names = ["nothing"] + [path_text(layer_path(flat)) for flat in layers["flat"]]
    pairs, counts = np.unique(tool[wrong].astype(np.int64) * len(names) + truth[wrong], return_counts=True)
    for pair, count in sorted(zip(pairs, counts), key=lambda item: -item[1])[:8]:
        print(f"  {count:>7,}  tool: {names[pair // len(names)]}   artist: {names[pair % len(names)]}")
    # Left: the tool's flats. Right: the artist's. Red marks every pixel that differs.
    sheet = Image.new("RGB", (2 * top.shape[1], top.shape[0]))
    for panel, flats in enumerate((tool, truth)):
        image = colors[flats].astype(np.float32)
        image[ink >= 55] *= 0.35
        image[ndi.maximum_filter(wrong, size=3)] = (255, 0, 0)
        sheet.paste(Image.fromarray(image.astype(np.uint8)), (panel * top.shape[1], 0))
    with open(output, "xb") as handle:  # Exclusive: never follows or replaces a file put there meanwhile.
        try:
            sheet.save(handle, format="PNG")
        except BaseException:
            handle.close()
            output.unlink()
            raise
    print(f"Differences: {output}")


def polish(source, output=None, max_megapixels=MAX_MEGAPIXELS):
    output = output or source.with_name(f"{source.stem}-polished.psd")
    if output.suffix.lower() != ".psd":
        raise ValueError("The output filename must end in .psd.")
    preview = output.with_name(f"{output.stem}-review.png")
    for path in (output, preview):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"{path} already exists. Choose another --output name.")
    print("Reading flats and lineart…", flush=True)
    psd, layers, _, _, corrections, uncertain = load(source, max_megapixels)
    # The review panels skip the file's ICC profile: they compare pixels, not print colour.
    before = psd.composite(alpha=1, ignore_preview=True, apply_icc=False).convert("RGB")
    changed = apply_corrections(psd, layers["flat"], corrections)
    print("Rendering and verifying the PSD copy…", flush=True)
    after = psd.composite(alpha=1, ignore_preview=True, apply_icc=False).convert("RGB")
    profile = psd.image_resources.get_data(Resource.ICC_PROFILE)
    with TemporaryDirectory(prefix=".color-cleanup-", dir=output.parent) as stage:
        staged_psd, staged_preview = Path(stage)/output.name, Path(stage)/preview.name
        psd.save(staged_psd)
        reopened = PSDImage.open(staged_psd)
        if reopened.size != psd.size or reopened.image_resources.get_data(Resource.ICC_PROFILE) != profile:
            raise ValueError("Saved PSD failed its canvas or color profile check.")
        preview_sheet(before, after, corrections, uncertain, staged_preview)
        publish(staged_psd, output)
        try:
            publish(staged_preview, preview)
        except BaseException:  # Both files or neither, even on Ctrl-C.
            output.unlink()
            raise
    print(f"Corrected {np.count_nonzero(corrections >= 0):,} pixels across {changed} layers. "
          f"Filled {np.count_nonzero(uncertain):,} pocket pixels to inspect.")
    print(f"PSD: {output}\nReview: {preview}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("layers", help="list visible raster layers with suggested roles")
    listing.add_argument("source", type=Path)
    listing.add_argument("--json", action="store_true", help="print the table as JSON")
    prepare = commands.add_parser("setup", help="confirm layer roles and save artwork settings")
    prepare.add_argument("source", type=Path)
    for role in ROLES:
        prepare.add_argument(f"--{role}", metavar="N,N",
                             help="layer numbers from `layers`; - for none; omitted = the suggestion")
    prepare.add_argument("--accept", action="store_true",
                         help="no prompts: use the suggestion for every role not given")
    prepare.add_argument("--ignore", metavar="N,N", help="layer numbers to hide in the output copy")
    prepare.add_argument("--max-region-px", type=int, metavar="N", help="8 to 128; default 1%% of the canvas")
    prepare.add_argument("--protect-box", action="append", default=[], metavar="L,T,R,B",
                         help="keep bare canvas bare inside this rectangle; repeat for more")
    review = commands.add_parser("settings", help="print the saved settings that polish would apply")
    review.add_argument("source", type=Path)
    run = commands.add_parser("polish", help="write an editable PSD copy and review PNG")
    run.add_argument("source", type=Path)
    run.add_argument("--output", type=Path)
    check = commands.add_parser("compare", help="score the tool against a PSD you finished by hand")
    check.add_argument("source", type=Path)
    check.add_argument("reference", type=Path)
    check.add_argument("--output", type=Path)
    for command in (run, check):
        command.add_argument("--max-megapixels", type=float, default=MAX_MEGAPIXELS, metavar="N",
                             help=f"refuse larger canvases (default {MAX_MEGAPIXELS}; about "
                                  f"{BYTES_PER_PIXEL * 1e6 / 1e9:.2f} GB of memory per megapixel)")
    args = parser.parse_args(argv)
    try:
        if args.command == "layers":
            psd = read_psd(args.source)
            print_layers(layer_rows(psd), psd.size, args.json)
        elif args.command == "setup":
            choices = {role: getattr(args, role) for role in ROLES}
            interactive = not args.accept and all(value is None for value in choices.values())
            setup(args.source, None if interactive else choices, args.ignore, args.max_region_px,
                  [parse_box(box) for box in args.protect_box])
        elif args.command == "settings":
            show_settings(args.source)
        elif args.command == "compare":
            compare(args.source, args.reference, args.output, args.max_megapixels)
        else:
            polish(args.source, args.output, args.max_megapixels)
    except (EOFError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
