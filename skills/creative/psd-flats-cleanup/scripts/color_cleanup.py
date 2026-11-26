#!/usr/bin/env python3
"""Polish flats in a layered RGB PSD, preserving the source document."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import re
import sys

import numpy as np
from PIL import Image, ImageDraw
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer
from psd_tools.constants import BlendMode, ColorMode, Compression, Resource
from scipy import ndimage as ndi
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


def read_psd(path):
    psd = PSDImage.open(path)
    if psd.color_mode != ColorMode.RGB or psd.depth != 8:
        raise ValueError("Use an 8-bit RGB PSD with separate raster flats and lineart.")
    return psd


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
    if re.search(r"line\s*art|(?:^|/)line\d*(?:/|$)|ink|(?:^|[ /_])lines?(?:$|[ /_])", title):
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


def setup(source):
    psd = read_psd(source)
    shape = (psd.height, psd.width)
    rows = [(ref, suggested_role(layer, ref, shape)) for ref, layer in raster_layers(psd)
            if layer.is_visible() and layer.width and layer.height]
    print("Visible raster layers, bottom to top")
    for number, (ref, role) in enumerate(rows, 1):
        print(f"{number:>3}  {role:<10}  {' / '.join(ref['names'])}")
    roles = {}
    for role in ("lineart", "flat", "background", "protect"):
        defaults = [str(i) for i, (_, guess) in enumerate(rows, 1) if guess == role]
        while True:
            answer = input(f"{role} layer numbers [{','.join(defaults) or 'none'}] "
                           "(Enter accepts, - clears): ").strip()
            try:
                ids = [] if answer == "-" else [int(i) for i in (answer.split(",") if answer else defaults)]
                if len(ids) != len(set(ids)) or any(i < 1 or i > len(rows) for i in ids):
                    raise ValueError
                if role in ("lineart", "flat") and not ids:
                    raise ValueError
                roles[role] = [rows[i-1][0] for i in ids]
                break
            except ValueError:
                print("Enter comma-separated layer numbers from the table.")
    config = {"layers": roles, "protect_boxes": []}
    resolve_config(psd, config)
    with config_path(source).open("x", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Saved {config_path(source)}")


def resolve_config(psd, config):
    available = {tuple(ref["index"]): (ref, layer) for ref, layer in raster_layers(psd)}
    used = set()
    resolved = {}
    for role in ("lineart", "flat", "background", "protect"):
        selections = []
        for ref in config["layers"].get(role, []):
            path = tuple(ref["index"])
            found = available.get(path)
            if found is None or found[0]["names"] != ref["names"] or not found[1].is_visible():
                name = " / ".join(ref["names"])
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
    for x0, y0, x1, y1 in boxes:
        if not (0 <= x0 < x1 <= shape[1] and 0 <= y0 < y1 <= shape[0]):
            raise ValueError("Each protect_boxes entry must be [left, top, right, bottom] within the canvas.")
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


def load(source):
    """Open the artwork and work out its corrections from the saved settings."""
    psd = read_psd(source)
    config = json.loads(config_path(source).read_text(encoding="utf-8"))
    layers = resolve_config(psd, config)
    available = {tuple(ref["index"]): (ref, layer) for ref, layer in raster_layers(psd)}
    for ref in config.get("ignore", []):
        found = available.get(tuple(ref["index"]))
        if found is None or found[0]["names"] != ref["names"]:
            raise ValueError(f"Ignored layer moved or changed name: {' / '.join(ref['names'])}.")
        if any(ref in refs for refs in config["layers"].values()):
            raise ValueError(f"An ignored layer cannot also have a role: {' / '.join(ref['names'])}.")
        found[1].visible = False
    shape = (psd.height, psd.width)
    top, solid, shield = read_paint(shape, layers["flat"], layers["protect"], config.get("protect_boxes", []))
    ink = read_ink(shape, layers["lineart"])
    island = config.get("max_region_px", max(8, min(64, round(min(shape)/100))))
    if type(island) is not int or not 8 <= island <= 128:
        raise ValueError("max_region_px must be an integer from 8 to 128.")
    print("Matching paint to the lines…", flush=True)
    corrections, pockets = correction_map(top, solid, shield, ink, len(layers["flat"]) + 1, island)
    return psd, layers, top, ink, corrections, pockets


def compare(source, reference, output=None):
    """Score the tool against a copy of the artwork that the artist finished by hand."""
    output = output or source.with_name(f"{source.stem}-compare.png")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"{output} already exists. Choose another --output name.")
    print("Reading flats and lineart…", flush=True)
    psd, layers, top, ink, corrections, _ = load(source)
    finished = PSDImage.open(reference)
    if finished.size != psd.size:
        raise ValueError("The finished PSD must have the same canvas size as the source.")
    by_name = {}
    for _, layer in raster_layers(finished):
        by_name.setdefault(layer.name, layer)
    truth = np.zeros(top.shape, np.int16)
    dropped = []
    colors = np.full((len(layers["flat"]) + 1, 3), 255, np.uint8)
    for index, flat in enumerate(layers["flat"], 1):
        canvas, local = window(flat, top.shape)
        data = pixels(flat)[local]
        colors[index] = np.median(data[data[..., 3] >= 240, :3], axis=0)
        layer = by_name.get(f"{flat.name} (polished)") or by_name.get(flat.name)
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
    names = ["nothing"] + [flat.name for flat in layers["flat"]]
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
    sheet.save(output)
    print(f"Differences: {output}")


def polish(source, output=None):
    output = output or source.with_name(f"{source.stem}-polished.psd")
    if output.suffix.lower() != ".psd":
        raise ValueError("The output filename must end in .psd.")
    preview = output.with_name(f"{output.stem}-review.png")
    for path in (output, preview):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"{path} already exists. Choose another --output name.")
    print("Reading flats and lineart…", flush=True)
    psd, layers, _, _, corrections, uncertain = load(source)
    before = psd.composite(alpha=1, ignore_preview=True).convert("RGB")
    changed = apply_corrections(psd, layers["flat"], corrections)
    print("Rendering and verifying the PSD copy…", flush=True)
    after = psd.composite(alpha=1, ignore_preview=True).convert("RGB")
    profile = psd.image_resources.get_data(Resource.ICC_PROFILE)
    with TemporaryDirectory(prefix=".color-cleanup-", dir=output.parent) as stage:
        staged_psd, staged_preview = Path(stage)/output.name, Path(stage)/preview.name
        psd.save(staged_psd)
        reopened = PSDImage.open(staged_psd)
        if reopened.size != psd.size or reopened.image_resources.get_data(Resource.ICC_PROFILE) != profile:
            raise ValueError("Saved PSD failed its canvas or color profile check.")
        preview_sheet(before, after, corrections, uncertain, staged_preview)
        os.link(staged_psd, output)
        try:
            os.link(staged_preview, preview)
        except OSError:
            output.unlink()
            raise
    print(f"Corrected {np.count_nonzero(corrections >= 0):,} pixels across {changed} layers. "
          f"Filled {np.count_nonzero(uncertain):,} pocket pixels to inspect.")
    print(f"PSD: {output}\nReview: {preview}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="confirm layer roles and save artwork settings").add_argument("source", type=Path)
    run = commands.add_parser("polish", help="write an editable PSD copy and review PNG")
    run.add_argument("source", type=Path)
    run.add_argument("--output", type=Path)
    check = commands.add_parser("compare", help="score the tool against a PSD you finished by hand")
    check.add_argument("source", type=Path)
    check.add_argument("reference", type=Path)
    check.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup(args.source)
        elif args.command == "compare":
            compare(args.source, args.reference, args.output)
        else:
            polish(args.source, args.output)
    except (EOFError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
