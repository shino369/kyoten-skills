"""Tests for color_cleanup.py (run through uv; see SKILL.md "Tests").

The first cases are the original Coloring Helper tests by staryume, ported to pytest with the
same assertions. The rest cover every remaining branch.
"""

import errno
import hashlib
import io
import json
import os
import pathlib
import runpy
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageCms
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer
from psd_tools.constants import BlendMode, Resource
from psd_tools.psd.image_resources import ImageResource

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import color_cleanup as cc  # noqa: E402
from color_cleanup import correction_map, read_psd  # noqa: E402

PINK = (190, 116, 132)


def geometry(broken=False):
    ink = np.zeros((100, 130), dtype=np.uint8)
    ink[15:86, (19, 105)] = 255
    ink[(15, 85), 19:106] = 255
    ink[39:61, (65, 85)] = 255
    ink[(39, 60), 65:86] = 255
    owner = np.zeros(ink.shape, np.int16)
    owner[16:85, 20:105] = 1
    owner[40:60, 66:85] = 0
    owner[28:49, 20:24] = 0  # Gap inside the left contour.
    owner[28:49, 106:109] = 1  # Spill outside the right contour.
    if broken:
        ink[30:48, 19] = 0
    return owner, ink


def repair(owner, ink, shield=None):
    shield = np.zeros_like(owner, bool) if shield is None else shield
    return correction_map(owner, np.ones_like(owner, bool), shield, ink, 2, 8)[0]


def rgba(mask, color=PINK, alpha=255):
    data = np.zeros((*mask.shape, 4), np.uint8)
    data[mask] = (*color, alpha)
    return data


def artwork(path, owner=None, ink=None, profile=True):
    """Paper, then Drawing/Paint/Pass (flat) and Drawing/Contours/Pass (ink): the original
    end-to-end fixture. Layer table: 1 Paper, 2 flat, 3 ink."""
    if owner is None:
        owner, ink = geometry()
    psd = PSDImage.new("RGB", owner.shape[::-1])
    if profile:
        data = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        psd.image_resources[Resource.ICC_PROFILE] = ImageResource(key=Resource.ICC_PROFILE, data=data)
    psd.create_pixel_layer(Image.new("RGB", psd.size, "white"), name="Paper")
    frame = psd.create_group(name="Drawing")
    pigments = frame.create_group(name="Paint")
    PixelLayer.fromarray(rgba(owner == 1), pigments, name="Pass")
    outlines = frame.create_group(name="Contours")
    trace = np.zeros((*ink.shape, 4), np.uint8)
    trace[..., 3] = ink
    PixelLayer.fromarray(trace, outlines, name="Pass")
    psd.save(path)
    return path


def configure(source, lineart="3", flat="2", background="1", protect="-"):
    cc.setup(source, {"lineart": lineart, "flat": flat, "background": background, "protect": protect})
    return json.loads(cc.config_path(source).read_text(encoding="utf-8"))


def fake(name="Layer", data=None, visible=True, blend=BlendMode.NORMAL, size=(8, 8)):
    """A stand-in raster layer for the role guesses."""
    data = np.zeros((8, 8, 4), np.uint8) if data is None else data
    return SimpleNamespace(name=name, width=size[0], height=size[1], size=size, blend_mode=blend,
                           is_visible=lambda: visible, topil=lambda apply_icc: Image.fromarray(data, "RGBA"))


def ref(*names):
    return {"index": list(range(len(names))), "names": list(names)}


# ---------- the original Coloring Helper tests ----------


def test_fills_gap_removes_spill_and_keeps_wide_opening():
    owner, ink = geometry()
    fix = repair(owner, ink)
    assert fix[36, 22] == 1
    assert fix[36, 107] == 0
    assert fix[50, 75] == -1
    assert fix[80, 40] == -1
    assert fix[5, 5] == -1


def test_paint_meets_the_middle_of_the_line():
    owner, ink = geometry()
    ink[15:86, 17:20] = 255
    fix = repair(owner, ink)
    assert fix[60, 19] == 1
    assert fix[60, 17] == -1


def test_fills_strand_tip_through_a_broken_contour():
    owner, ink = geometry(broken=True)
    owner[28:49, 20:24] = 1
    owner[73:85, 20:60] = 0  # Paint stops short of a corner the lines still close.
    fix = repair(owner, ink)
    assert (fix[75:84, 22:58] == 1).all()
    assert fix[40, 10] == -1


def test_fills_pinhole_and_keeps_unlined_shape():
    owner, ink = geometry()
    owner[70:72, 30:32] = 0
    owner[62:76, 86:100] = 0  # A deliberate shape with no lines around it.
    fix = repair(owner, ink)
    assert fix[70, 30] == 1
    assert fix[68, 92] == -1


def test_canvas_edge_is_open_and_all_ink_changes_nothing():
    owner = np.zeros((30, 30), np.int16)
    owner[3:, :] = 1
    ink = np.zeros((30, 30), np.uint8)
    ink[2, :] = 255
    assert (repair(owner, ink)[:2] == -1).all()
    assert (repair(np.ones((4, 4), np.int16), np.full((4, 4), 255, np.uint8)) == -1).all()


def test_keeps_soft_edge_of_unlined_shape():
    owner, ink = geometry()
    solid = np.ones_like(owner, bool)
    solid[70:75, 40:60] = False
    fix = correction_map(owner, solid, np.zeros_like(owner, bool), ink, 2, 8)[0]
    assert (fix[70:75, 40:60] == -1).all()


def test_leaves_protected_area_alone():
    owner, ink = geometry()
    shield = np.zeros_like(owner, bool)
    shield[25:52, 15:30] = True
    fix = repair(owner, ink, shield)
    assert (fix[28:49, 20:24] == -1).all()
    assert fix[36, 107] == 0


def test_setup_and_polish_nested_psd(tmp_path, monkeypatch, capsys):
    source = artwork(tmp_path / "Other artwork.psd")
    profile = read_psd(source).image_resources.get_data(Resource.ICC_PROFILE)
    original_hash = hashlib.sha256(source.read_bytes()).digest()
    answers = iter(["3", "2", "1", "-"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    cc.main(["setup", str(source)])
    cc.main(["polish", str(source)])
    result = source.with_name("Other artwork-polished.psd")
    out = read_psd(result)
    assert out.size == (130, 100)
    assert out.image_resources.get_data(Resource.ICC_PROFILE) == profile
    assert [layer.name for layer in out[1][0]] == ["Pass", "Pass (polished)"]
    assert not out[1][0][0].visible
    assert out[1][0][1].visible
    assert out[1][1][0].visible
    rendered = out.composite().convert("RGB")
    assert rendered.getpixel((22, 36)) == PINK
    assert rendered.getpixel((107, 36)) == (255, 255, 255)
    assert rendered.getpixel((75, 50)) == (255, 255, 255)
    assert hashlib.sha256(source.read_bytes()).digest() == original_hash
    capsys.readouterr()
    cc.main(["compare", str(source), str(result)])
    scored = capsys.readouterr().out
    assert "matches 100.0%" in scored
    assert "0 pixels still differ" in scored
    assert source.with_name("Other artwork-compare.png").exists()
    # RLE (1), never ZIP (2/3): Clip Studio Paint showed ZIP layers blank.
    assert {c.compression for layer in out.descendants() for c in layer._channels} <= {0, 1}
    with pytest.raises(SystemExit) as refused:
        cc.main(["polish", str(source)])
    assert refused.value.code == 1
    rerun = source.with_name("Other artwork-rerun.psd")
    cc.main(["polish", str(source), "--output", str(rerun)])
    assert rerun.exists()


# ---------- layer listing and setup ----------


def test_layers_lists_table_and_json(tmp_path, capsys):
    source = artwork(tmp_path / "art.psd")
    cc.main(["layers", str(source)])
    table = capsys.readouterr().out
    assert "1  background  Paper" in table
    assert "3  lineart     Drawing / Contours / Pass" in table
    assert "Canvas 130×100 (0.0 MP)" in table
    cc.main(["layers", str(source), "--json"])
    listing = json.loads(capsys.readouterr().out)
    assert listing["canvas"] == {"width": 130, "height": 100, "megapixels": 0.013, "memory_gb": 0.0}
    rows = listing["layers"]
    assert [(r["number"], r["suggested"], r["names"]) for r in rows] == [
        (1, "background", ["Paper"]),
        (2, "flat", ["Drawing", "Paint", "Pass"]),
        (3, "lineart", ["Drawing", "Contours", "Pass"]),
    ]
    assert rows[1]["index"] == [1, 0, 0]


def test_setup_accept_uses_suggestions_and_refuses_rerun(tmp_path, capsys):
    source = artwork(tmp_path / "art.psd")
    cc.main(["setup", str(source), "--accept"])
    assert "protect     -" in capsys.readouterr().out
    config = json.loads(cc.config_path(source).read_text(encoding="utf-8"))
    assert [r["names"][-1] for r in config["layers"]["lineart"]] == ["Pass"]
    assert config["layers"]["background"][0]["names"] == ["Paper"]
    assert config["protect_boxes"] == []
    with pytest.raises(SystemExit):
        cc.main(["setup", str(source), "--accept"])
    assert "already exists" in capsys.readouterr().err


def test_setup_flags_override_and_reject_bad_numbers(tmp_path, capsys):
    source = artwork(tmp_path / "art.psd")
    for flags, message in ((["--flat", "2,2"], "flat:"), (["--lineart", "9"], "lineart:"),
                           (["--lineart", "-"], "at least one"), (["--protect", "x"], "protect:")):
        with pytest.raises(SystemExit):
            cc.main(["setup", str(source), *flags])
        assert message in capsys.readouterr().err
    assert not cc.config_path(source).exists()
    config = configure(source, background="-")
    assert config["layers"]["background"] == []


def test_setup_interactive_retries_after_a_bad_answer(tmp_path, monkeypatch, capsys):
    source = artwork(tmp_path / "art.psd")
    answers = iter(["", "7", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    cc.setup(source)
    out = capsys.readouterr().out
    assert "Visible raster layers" in out and "flat: enter comma-separated" in out
    assert cc.config_path(source).exists()


def test_setup_rejects_one_layer_in_two_roles(tmp_path):
    source = artwork(tmp_path / "art.psd")
    with pytest.raises(ValueError, match="exactly one role"):
        configure(source, lineart="3", flat="2", background="2")


# ---------- reading the document ----------


def test_read_psd_rejects_other_modes(tmp_path):
    path = tmp_path / "deep.psd"
    PSDImage.new("RGB", (4, 4), depth=16).save(path)
    with pytest.raises(ValueError, match="8-bit RGB"):
        read_psd(path)


def test_raster_layers_skips_non_pixel_layers():
    leaf = SimpleNamespace(name="Text", kind="type", is_group=lambda: False)
    assert list(cc.raster_layers([leaf])) == []


def test_pixels_and_window_errors():
    with pytest.raises(ValueError, match="no raster data"):
        cc.pixels(SimpleNamespace(name="Empty", topil=lambda apply_icc: None))
    with pytest.raises(ValueError, match="inside the canvas"):
        cc.window(SimpleNamespace(name="Away", bbox=(50, 50, 60, 60)), (10, 10))
    canvas, local = cc.window(SimpleNamespace(name="Edge", bbox=(-2, -3, 5, 4)), (10, 10))
    assert canvas == (slice(0, 4), slice(0, 5)) and local == (slice(3, 7), slice(2, 7))


def solid_block(color, rows, columns):
    """Solid paint in the top-left corner; the role guess samples every 8th pixel."""
    data = np.zeros((64, 64, 4), np.uint8)
    data[:rows, :columns] = (*color, 255)
    return data


@pytest.mark.parametrize("layer, expected", [
    (fake(visible=False), "skip"),
    (fake(size=(0, 8)), "skip"),
    (fake("Eye white"), "protect"),
    (fake("Lineart"), "lineart"),
    (fake("BG"), "background"),
    (fake("Layer 3", blend=BlendMode.MULTIPLY), "lineart"),
    (fake("Base colour"), "flat"),
    (fake(data=np.full((64, 64, 4), 255, np.uint8), size=(64, 64)), "background"),
    (fake(data=solid_block((20, 20, 20), 32, 64), size=(64, 64)), "flat"),
    (fake(data=solid_block((20, 20, 20), 1, 1), size=(64, 64)), "lineart"),
    (fake(data=solid_block((200, 180, 170), 1, 1), size=(64, 64)), "flat"),
])
def test_suggested_role(layer, expected):
    assert cc.suggested_role(layer, ref(layer.name), (64, 64)) == expected


def test_suggested_role_mixed_and_sparse_layers():
    mixed = np.zeros((64, 64, 4), np.uint8)
    for column in range(8):  # Eight colours, none dominant.
        mixed[:, column * 8:(column + 1) * 8] = (column * 30, 90, 40, 255)
    assert cc.suggested_role(fake(data=mixed, size=(32, 32)), ref("Mix"), (64, 64)) == "skip"
    sparse = np.zeros((64, 64, 4), np.uint8)
    sparse[:4, :, 3] = 120  # Soft, translucent: not solid at all.
    assert cc.suggested_role(fake(data=sparse, size=(64, 64)), ref("Soft"), (64, 64)) == "lineart"


def test_resolve_config_rejects_changed_layers(tmp_path):
    source = artwork(tmp_path / "art.psd")
    config = configure(source)
    psd = read_psd(source)
    renamed = json.loads(json.dumps(config))
    renamed["layers"]["flat"][0]["names"][-1] = "Other"
    with pytest.raises(ValueError, match="changed name"):
        cc.resolve_config(psd, renamed)
    empty = json.loads(json.dumps(config))
    empty["layers"]["lineart"] = []
    with pytest.raises(ValueError, match="at least one"):
        cc.resolve_config(psd, empty)


@pytest.mark.parametrize("change, message", [
    (lambda flat, group: setattr(flat, "opacity", 128), "fully opaque Normal"),
    (lambda flat, group: setattr(flat, "clipping", True), "without masks"),
    (lambda flat, group: setattr(group, "opacity", 128), "Pass Through flat groups"),
])
def test_resolve_config_rejects_flats_that_do_not_composite_plainly(tmp_path, change, message):
    source = artwork(tmp_path / "art.psd")
    config = configure(source)
    psd = read_psd(source)
    group = psd[1][0]
    change(group[0], group)
    with pytest.raises(ValueError, match=message):
        cc.resolve_config(psd, config)


def test_resolve_config_rejects_background_above_flats(tmp_path):
    source = artwork(tmp_path / "art.psd")
    config = configure(source)
    psd = read_psd(source)
    config["layers"]["background"], config["layers"]["lineart"] = (
        config["layers"]["lineart"], config["layers"]["background"])
    with pytest.raises(ValueError, match="below the editable flats"):
        cc.resolve_config(psd, config)


def flat_layer(alpha, bbox=(0, 0, 4, 4)):
    data = np.zeros((bbox[3] - bbox[1], bbox[2] - bbox[0], 4), np.uint8)
    data[..., 3] = alpha
    return SimpleNamespace(name="Flat", bbox=bbox, topil=lambda apply_icc: Image.fromarray(data, "RGBA"))


def test_read_paint_protects_layers_and_boxes():
    flat = flat_layer(255, (0, 0, 2, 4))
    shield_layer = flat_layer(255, (3, 3, 4, 4))
    top, solid, shield = cc.read_paint((4, 4), [flat], [shield_layer], [[2, 0, 4, 1]])
    assert top[0, 0] == 1 and solid.all()
    assert shield[3, 3] and shield[0, 3]
    with pytest.raises(ValueError, match="no solid color"):
        cc.read_paint((4, 4), [flat_layer(100)], [], [])
    with pytest.raises(ValueError, match="protect_boxes"):
        cc.read_paint((4, 4), [flat], [], [[0, 0, 5, 1]])


def test_read_ink_from_opaque_scans_and_multiply_layers():
    paper = np.full((2, 2, 4), 255, np.uint8)
    paper[0, 0, :3] = 0
    scan = SimpleNamespace(name="Scan", bbox=(0, 0, 2, 2), opacity=255, blend_mode=BlendMode.NORMAL,
                           topil=lambda apply_icc: Image.fromarray(paper, "RGBA"))
    ink = cc.read_ink((2, 2), [scan])
    assert ink[0, 0] == 255 and ink[1, 1] == 0
    scan.blend_mode = BlendMode.MULTIPLY
    assert (cc.read_ink((2, 2), [scan]) == ink).all()


# ---------- corrections ----------


def test_paint_hidden_under_ink_and_regions_too_thin_for_a_core():
    top = np.zeros((6, 6), np.int16)
    top[:, 3:] = 1
    free = np.ones((6, 6), bool)
    free[:, 3:] = False  # Every flat-1 pixel is under ink.
    regions, kind, half = cc.paint_regions(top, free, 2, 5)
    assert set(kind) == {0}
    assert regions[:, :3].min() > 0 and (regions[:, 3:] == 0).all()


def test_correction_cascade_runs_every_pass():
    """Three equal cells (paints 2, 3, 2) in a lined channel off a wide paint-1 area: each of
    the three passes claims one more cell, because only a cell joined to it counts as wide."""
    ink = np.full((60, 170), 255, np.uint8)
    ink[6:54, 6:80] = 0
    ink[10:50, 80:152] = 0
    owner = np.zeros(ink.shape, np.int16)
    owner[6:54, 6:80] = 1
    for paint, x in ((2, 80), (3, 104), (2, 128)):
        owner[10:50, x:x + 24] = paint
    fix = correction_map(owner, np.ones_like(owner, bool), np.zeros_like(owner, bool), ink, 4, 16)[0]
    assert (fix[14:46, 84:148] == 1).all()


def test_round_paint_has_no_slivers():
    yy, xx = np.mgrid[:41, :41]
    top = (np.hypot(yy - 20, xx - 20) <= 12).astype(np.int16)
    regions, kind, half = cc.paint_regions(top, np.ones(top.shape, bool), 2, 2)
    assert list(kind).count(1) == 1


def test_unpainted_canvas_has_no_pockets_to_fill():
    ink = np.zeros((40, 40), np.uint8)
    ink[(10, 30), 10:31] = 255
    ink[10:31, (10, 30)] = 255
    fix, pockets = correction_map(np.zeros((40, 40), np.int16), np.ones((40, 40), bool),
                                  np.zeros((40, 40), bool), ink, 2, 8)
    assert pockets.any() and (fix == -1).all()  # A pocket, but no paint to fill it from.


def test_apply_corrections_trims_without_adding_paint(tmp_path):
    source = artwork(tmp_path / "art.psd")
    configure(source)
    psd = read_psd(source)
    flats = [psd[1][0][0]]
    target = np.full((100, 130), -1, np.int16)
    target[30, 30] = 0
    assert cc.apply_corrections(psd, flats, target) == 1
    assert psd[1][0][1].name == "Pass (polished)"
    assert cc.apply_corrections(psd, flats, np.full((100, 130), -1, np.int16)) == 0


# ---------- load, polish, compare ----------


def test_load_settings_errors(tmp_path):
    source = artwork(tmp_path / "art.psd")
    config = configure(source)
    path = cc.config_path(source)

    def rewrite(**changes):
        path.write_text(json.dumps({**config, **changes}), encoding="utf-8")

    rewrite(ignore=[{"index": [9], "names": ["Gone"]}])
    with pytest.raises(ValueError, match="Ignored layer moved"):
        cc.load(source)
    rewrite(ignore=[config["layers"]["flat"][0]])
    with pytest.raises(ValueError, match="cannot also have a role"):
        cc.load(source)
    rewrite(max_region_px=4)
    with pytest.raises(ValueError, match="max_region_px"):
        cc.load(source)
    rewrite(max_region_px=True)
    with pytest.raises(ValueError, match="max_region_px"):
        cc.load(source)


def test_ignored_layer_is_hidden_in_the_copy(tmp_path):
    source = artwork(tmp_path / "art.psd")
    psd = read_psd(source)
    notes = np.zeros((100, 130, 4), np.uint8)
    notes[0:4, 0:4] = (0, 0, 255, 255)
    PixelLayer.fromarray(notes, psd, name="Notes")
    psd.save(source)
    config = configure(source, protect="-")
    config["ignore"] = [{"index": [2], "names": ["Notes"]}]
    config["max_region_px"] = 8
    cc.config_path(source).write_text(json.dumps(config), encoding="utf-8")
    cc.polish(source)
    out = read_psd(source.with_name("art-polished.psd"))
    assert not out[2].visible


def test_polish_refuses_bad_outputs(tmp_path):
    source = artwork(tmp_path / "art.psd")
    configure(source)
    with pytest.raises(ValueError, match="end in .psd"):
        cc.polish(source, tmp_path / "out.png")
    source.with_name("art-polished-review.png").write_bytes(b"")
    with pytest.raises(FileExistsError):
        cc.polish(source)


def test_polish_checks_the_saved_copy(tmp_path, monkeypatch):
    source = artwork(tmp_path / "art.psd")
    configure(source)
    real_open = PSDImage.open
    calls = []

    def reopen(path):
        calls.append(path)
        psd = real_open(path)
        if len(calls) > 1:  # The staged copy: pretend its profile was lost.
            del psd.image_resources[Resource.ICC_PROFILE]
        return psd

    monkeypatch.setattr(cc.PSDImage, "open", reopen)
    with pytest.raises(ValueError, match="color profile check"):
        cc.polish(source)
    assert not source.with_name("art-polished.psd").exists()


def test_polish_removes_the_psd_when_the_review_cannot_be_published(tmp_path, monkeypatch):
    source = artwork(tmp_path / "art.psd")
    configure(source)
    real_link = os.link

    def link(src, dst):
        if str(dst).endswith(".png"):
            raise OSError(errno.ENOSPC, "disk full")
        real_link(src, dst)

    monkeypatch.setattr(cc.os, "link", link)
    real_replace = os.replace

    def replace(src, dst):
        if str(dst).endswith(".png"):
            raise OSError(errno.ENOSPC, "disk full")
        real_replace(src, dst)

    monkeypatch.setattr(cc.os, "replace", replace)
    with pytest.raises(OSError, match="disk full"):
        cc.polish(source)
    assert not source.with_name("art-polished.psd").exists()
    assert not source.with_name("art-polished-review.png").exists()
    assert not list(tmp_path.glob(".color-cleanup-*"))


def test_polish_rolls_back_on_interrupt(tmp_path, monkeypatch):
    source = artwork(tmp_path / "art.psd")
    configure(source)
    real_link = os.link

    def link(src, dst):
        if str(dst).endswith(".png"):
            raise KeyboardInterrupt
        real_link(src, dst)

    monkeypatch.setattr(cc.os, "link", link)
    with pytest.raises(KeyboardInterrupt):
        cc.polish(source)
    assert not source.with_name("art-polished.psd").exists()
    assert not list(tmp_path.glob(".color-cleanup-*"))


def test_polish_copies_where_the_volume_has_no_hard_links(tmp_path, monkeypatch, capsys):
    source = artwork(tmp_path / "art.psd")
    configure(source)

    def link(src, dst):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(cc.os, "link", link)
    cc.polish(source)
    assert read_psd(source.with_name("art-polished.psd")).size == (130, 100)
    assert Image.open(source.with_name("art-polished-review.png")).size[0] == 3 * 130
    out = capsys.readouterr().out
    assert "flat        Drawing / Paint / Pass" in out  # The settings it applied.
    assert "max_region_px 8" in out


def test_publish_refuses_an_existing_file_even_without_hard_links(tmp_path, monkeypatch):
    staged = tmp_path / "staged.bin"
    staged.write_bytes(b"new")
    target = tmp_path / "target.bin"
    target.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        cc.publish(staged, target)
    monkeypatch.setattr(cc.os, "link", lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    with pytest.raises(FileExistsError):
        cc.publish(staged, target)
    assert not (tmp_path / "missing").exists()


def test_compare_scores_differences_and_skips_dropped_flats(tmp_path, capsys):
    owner, ink = geometry()
    source = artwork(tmp_path / "art.psd", owner, ink)
    configure(source)
    finished = artwork(tmp_path / "done.psd", owner, ink)
    capsys.readouterr()
    cc.compare(source, finished)  # A finished copy with nothing corrected by hand.
    out = capsys.readouterr().out
    assert "tool: nothing   artist: Drawing / Paint / Pass" in out
    with pytest.raises(FileExistsError):
        cc.compare(source, finished)
    psd = read_psd(finished)
    psd[1][0][0].visible = False
    psd.save(finished)
    cc.compare(source, finished, tmp_path / "dropped.png")
    assert "0 pixels still differ" in capsys.readouterr().out


def test_compare_needs_the_same_canvas(tmp_path):
    source = artwork(tmp_path / "art.psd")
    configure(source)
    other = tmp_path / "small.psd"
    PSDImage.new("RGB", (10, 10)).save(other)
    with pytest.raises(ValueError, match="same canvas size"):
        cc.compare(source, other)


def test_runs_as_a_script(tmp_path, monkeypatch, capsys):
    source = artwork(tmp_path / "art.psd")
    monkeypatch.setattr(sys, "argv", ["color_cleanup.py", "layers", str(source)])
    runpy.run_path(str(SCRIPTS / "color_cleanup.py"), run_name="__main__")
    assert "Visible raster layers" in capsys.readouterr().out


# ---------- review round 1 and security review ----------


def two_hair_flats(path):
    """Girl/Hair and Boy/Hair: two flats with the same layer name in different groups."""
    owner, ink = geometry()
    psd = PSDImage.new("RGB", owner.shape[::-1])
    psd.create_pixel_layer(Image.new("RGB", psd.size, "white"), name="Paper")
    left = np.zeros(owner.shape, bool)
    left[:, :62] = True
    for name, part, color in (("Girl", left, PINK), ("Boy", ~left, (60, 90, 170))):
        group = psd.create_group(name=name)
        PixelLayer.fromarray(rgba((owner == 1) & part, color), group, name="Hair")
    lines = np.zeros((*ink.shape, 4), np.uint8)
    lines[..., 3] = ink
    PixelLayer.fromarray(lines, psd, name="Ink")
    psd.save(path)
    return path


def test_compare_matches_flats_by_their_group_path(tmp_path, capsys):
    source = two_hair_flats(tmp_path / "pair.psd")
    configure(source, lineart="4", flat="2,3", background="1")
    cc.polish(source)
    capsys.readouterr()
    cc.compare(source, source.with_name("pair-polished.psd"))
    out = capsys.readouterr().out
    assert "matches 100.0%" in out and "0 pixels still differ" in out


def test_compare_refuses_an_ambiguous_layer_path(tmp_path):
    source = two_hair_flats(tmp_path / "pair.psd")
    configure(source, lineart="4", flat="2,3", background="1")
    finished = read_psd(source)
    finished[1].name = "Boy"  # Now both groups are "Boy": Boy / Hair twice.
    finished.save(tmp_path / "done.psd")
    with pytest.raises(ValueError, match="share the path Boy / Hair"):
        cc.compare(source, tmp_path / "done.psd")


def test_compare_writes_only_a_new_png(tmp_path, monkeypatch):
    source = artwork(tmp_path / "art.psd")
    configure(source)
    with pytest.raises(ValueError, match="end in .png"):
        cc.compare(source, source, tmp_path / "diff.psd")
    with pytest.raises(ValueError, match="8-bit RGB"):
        deep = tmp_path / "deep.psd"
        PSDImage.new("RGB", (130, 100), depth=16).save(deep)
        cc.compare(source, deep)

    def broken(self, fp, *args, **kwargs):
        fp.write(b"partial")
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(cc.Image.Image, "save", broken)
    with pytest.raises(OSError, match="disk full"):
        cc.compare(source, source, tmp_path / "diff.png")
    assert not (tmp_path / "diff.png").exists()


@pytest.mark.parametrize("name, expected", [
    ("Pink ribbon", "flat"), ("Blink", "flat"), ("Ink", "lineart"), ("inking 2", "lineart"), ("Inks", "lineart"),
])
def test_ink_is_a_whole_word(name, expected):
    light = solid_block((230, 170, 190), 32, 64)
    assert cc.suggested_role(fake(name, light, size=(64, 64)), ref(name), (64, 64)) == expected


@pytest.mark.parametrize("change, message", [
    (lambda c: c.update(protect_boxes=[[10.5, 0, 40, 40]]), "whole pixels"),
    (lambda c: c.update(protect_boxes="all"), "whole pixels"),
    (lambda c: c["layers"]["flat"][0].update(index=1), "as setup wrote it"),
    (lambda c: c["layers"]["flat"][0].update(extra=0), "as setup wrote it"),
    (lambda c: c["layers"].update(lineart="Ink"), "list of layers"),
    (lambda c: c["layers"].update(paint=[]), "Unknown role"),
    (lambda c: c.update(layers=[]), "\"layers\" object"),
    (lambda c: c.update(colour="red"), "Unknown setting"),
    (lambda c: c.update(ignore={"index": [0]}), "ignore"),
])
def test_settings_with_wrong_types_stop_with_a_message(tmp_path, capsys, change, message):
    source = artwork(tmp_path / "art.psd")
    config = configure(source)
    change(config)
    cc.config_path(source).write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit):
        cc.main(["polish", str(source)])
    assert message in capsys.readouterr().err


def test_ignore_is_matched_by_position_not_by_the_whole_entry(tmp_path):
    source = artwork(tmp_path / "art.psd")
    config = configure(source)
    config["ignore"] = [{**config["layers"]["lineart"][0]}]
    config["ignore"][0]["names"] = ["Drawing", "Contours", "Other"]
    cc.config_path(source).write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="Ignored layer moved"):
        cc.load(source)
    config["ignore"][0]["names"] = ["Drawing", "Contours", "Pass"]
    cc.config_path(source).write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot also have a role"):
        cc.load(source)


def test_setup_flags_write_every_setting(tmp_path, capsys):
    source = artwork(tmp_path / "art.psd")
    psd = read_psd(source)
    PixelLayer.fromarray(rgba(np.ones((100, 130), bool), (0, 0, 255)), psd, name="Notes")
    psd.save(source)
    cc.main(["setup", str(source), "--lineart", "3", "--flat", "2", "--background", "1", "--protect", "-",
             "--ignore", "4", "--max-region-px", "12", "--protect-box", "0,0,10,10", "--protect-box", "20,20,30,40"])
    config = json.loads(cc.config_path(source).read_text(encoding="utf-8"))
    assert config["ignore"] == [{"index": [2], "names": ["Notes"]}]
    assert config["max_region_px"] == 12
    assert config["protect_boxes"] == [[0, 0, 10, 10], [20, 20, 30, 40]]
    assert "ignore      Notes" in capsys.readouterr().out


@pytest.mark.parametrize("flags, message", [
    (["--ignore", "3"], "cannot also have a role"),
    (["--ignore", "9"], "ignore:"),
    (["--max-region-px", "200"], "max_region_px"),
    (["--protect-box", "0,0,500,10"], "protect_boxes"),
    (["--protect-box", "0,0,10"], "protect_boxes"),
    (["--protect-box", "a,b,c,d"], "protect_boxes"),
])
def test_setup_rejects_bad_settings_flags(tmp_path, capsys, flags, message):
    source = artwork(tmp_path / "art.psd")
    with pytest.raises(SystemExit):
        cc.main(["setup", str(source), "--accept", *flags])
    assert message in capsys.readouterr().err
    assert not cc.config_path(source).exists()


def test_large_canvases_need_an_explicit_memory_allowance(tmp_path, capsys):
    source = artwork(tmp_path / "art.psd")
    configure(source)
    with pytest.raises(SystemExit):
        cc.main(["polish", str(source), "--max-megapixels", "0.01"])
    assert "--max-megapixels 1" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cc.main(["compare", str(source), str(source), "--max-megapixels", "0.01"])
    cc.main(["polish", str(source), "--max-megapixels", "0.02"])
    assert source.with_name("art-polished.psd").exists()


def test_names_are_escaped_when_printed(capsys):
    rows = [(ref("線画\u3000a\nb\x1b[2K\u202e"), "lineart")]
    cc.print_layers(rows, (8, 8))
    out = capsys.readouterr().out
    assert "線画\u3000a\\nb\\x1b[2K\\u202e" in out
    assert "\x1b" not in out and "\u202e" not in out
    cc.print_layers(rows, (8, 8), as_json=True)
    listing = capsys.readouterr().out
    assert "\x1b" not in listing and "\u202e" not in listing
    assert json.loads(listing)["layers"][0]["names"] == ["線画\u3000a\nb\x1b[2K\u202e"]


def test_settings_prints_what_polish_would_apply(tmp_path, capsys):
    source = artwork(tmp_path / "art.psd")
    configure(source)
    capsys.readouterr()
    cc.main(["settings", str(source)])
    out = capsys.readouterr().out
    assert "Settings from art.cleanup.json" in out and "lineart     Drawing / Contours / Pass" in out
    config = json.loads(cc.config_path(source).read_text(encoding="utf-8"))
    config["ignore"] = config["layers"]["flat"]
    cc.config_path(source).write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit):
        cc.main(["settings", str(source)])
    assert "cannot also have a role" in capsys.readouterr().err


# ---------- review round 2 ----------


def test_polish_keeps_japanese_flat_names(tmp_path):
    owner, ink = geometry()
    source = artwork(tmp_path / "jp.psd", owner, ink)
    psd = read_psd(source)
    psd[1][0][0].name = "下塗り"
    psd.save(source)
    configure(source)
    cc.polish(source)
    out = read_psd(source.with_name("jp-polished.psd"))
    assert [layer.name for layer in out[1][0]] == ["下塗り", "下塗り (polished)"]


def test_polished_name_stays_within_the_psd_limit(tmp_path):
    source = artwork(tmp_path / "long.psd")
    psd = read_psd(source)
    psd[1][0][0].name = "x" * 250
    psd.save(source)
    configure(source)
    cc.polish(source)
    name = read_psd(source.with_name("long-polished.psd"))[1][0][1].name
    assert len(name) == 255 and name.endswith("x (polished)")


def two_flats_one_group(path):
    owner, ink = geometry()
    psd = PSDImage.new("RGB", owner.shape[::-1])
    psd.create_pixel_layer(Image.new("RGB", psd.size, "white"), name="Paper")
    group = psd.create_group(name="Paint")
    left = np.zeros(owner.shape, bool)
    left[:, :62] = True
    PixelLayer.fromarray(rgba((owner == 1) & left), group, name="Hair")
    PixelLayer.fromarray(rgba((owner == 1) & ~left, (60, 90, 170)), group, name="Hair")
    lines = np.zeros((*ink.shape, 4), np.uint8)
    lines[..., 3] = ink
    PixelLayer.fromarray(lines, psd, name="Ink")
    psd.save(path)
    return path


def test_compare_refuses_two_flats_with_one_path(tmp_path):
    source = two_flats_one_group(tmp_path / "same.psd")
    configure(source, lineart="4", flat="2,3", background="1")
    with pytest.raises(ValueError, match="Two flats share the path Paint / Hair"):
        cc.compare(source, source)


def test_compare_names_the_flats_it_did_not_score(tmp_path, capsys):
    source = two_hair_flats(tmp_path / "pair.psd")
    configure(source, lineart="4", flat="2,3", background="1")
    finished = read_psd(source)
    finished[1].name = "Girls"
    finished.save(tmp_path / "moved.psd")
    capsys.readouterr()
    cc.compare(source, tmp_path / "moved.psd")
    assert "Not scored (hidden, or not found at the same path): Girl / Hair" in capsys.readouterr().out


def test_output_never_fails_on_a_narrow_console_encoding(monkeypatch):
    narrow = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", narrow)
    monkeypatch.setattr(sys, "stderr", io.StringIO())  # Not a TextIOWrapper: left as it is.
    cc.safe_console()
    print("下塗り")
    narrow.flush()
    assert narrow.buffer.getvalue().strip() == b"\\u4e0b\\u5857\\u308a"


def test_an_optional_role_error_does_not_ask_for_one(tmp_path, capsys):
    source = artwork(tmp_path / "art.psd")
    with pytest.raises(SystemExit):
        cc.main(["setup", str(source), "--accept", "--ignore", "9"])
    assert "at least one" not in capsys.readouterr().err


def test_settings_runs_every_check_that_polish_runs(tmp_path, capsys):
    source = artwork(tmp_path / "art.psd")
    config = configure(source)
    for change, message in ((dict(protect_boxes=[[0, 0, 500, 10]]), "within the canvas"),
                            (dict(ignore=[{"index": [9], "names": ["Gone"]}]), "Ignored layer moved")):
        cc.config_path(source).write_text(json.dumps({**config, **change}), encoding="utf-8")
        with pytest.raises(SystemExit):
            cc.main(["settings", str(source)])
        assert message in capsys.readouterr().err


def test_publish_without_hard_links_never_leaves_a_partial_file(tmp_path, monkeypatch):
    staged = tmp_path / "stage" / "out.psd"
    staged.parent.mkdir()
    staged.write_bytes(b"whole file")
    target = tmp_path / "out.psd"
    monkeypatch.setattr(cc.os, "link", lambda a, b: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "no links")))
    monkeypatch.setattr(cc.os, "replace", lambda a, b: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        cc.publish(staged, target)
    assert not target.exists() and staged.read_bytes() == b"whole file"
