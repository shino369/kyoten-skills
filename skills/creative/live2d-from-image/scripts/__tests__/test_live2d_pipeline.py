"""Tests for live2d_pipeline.py (100% branch coverage)."""

from __future__ import annotations

import json
import struct
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import live2d_pipeline as lp  # noqa: E402


def _make_valid_moc3_bytes(param_ids: list[str] | None = None) -> bytes:
    """Create a valid >=64-byte MOC3 binary (optionally with real count/param tables)."""
    if param_ids is None:
        return b"MOC3\x05\x00" + b"\x00" * 64
    # Header (64) + section table (80*4=320 -> 384) + count_info (32*4=128 -> 512) + param_ids (N*64)
    buf = bytearray(512 + len(param_ids) * 64)
    buf[0:4] = b"MOC3"
    buf[4] = 5
    buf[5] = 0
    struct.pack_into("<I", buf, 64 + 0 * 4, 384)  # sec[0] = count_info
    struct.pack_into("<I", buf, 64 + 50 * 4, 512)  # sec[50] = param IDs
    struct.pack_into("<6I", buf, 384, 1, 1, 1, 0, 1, len(param_ids))
    for idx, pid in enumerate(param_ids):
        raw_id = pid.encode("ascii")[:63]
        buf[512 + idx * 64 : 512 + idx * 64 + len(raw_id)] = raw_id
    return bytes(buf)


def _write_valid_moc3_package(
    out_dir: Path,
    *,
    stem: str = "hero",
    param_ids: list[str] | None = None,
    physics_outputs: list[str] | None = None,
    vtube: bool = False,
    groups: list[dict] | None = None,
) -> None:
    """Write a self-consistent .moc3 + .model3.json (+ textures/physics/etc.) package."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.moc3").write_bytes(_make_valid_moc3_bytes(param_ids))
    tex_dir = out_dir / f"{stem}.4096"
    tex_dir.mkdir(exist_ok=True)
    (tex_dir / "texture_00.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    phys_settings = []
    if physics_outputs is not None:
        phys_settings.append(
            {
                "Id": "PhysicsSetting1",
                "Input": [],
                "Output": [
                    {"Destination": {"Target": "Parameter", "Id": pid}, "VertexIndex": 1, "Scale": 1.0}
                    for pid in physics_outputs
                ],
                "Vertices": [],
            }
        )
    (out_dir / f"{stem}.physics3.json").write_text(
        json.dumps({"Version": 3, "PhysicsSettings": phys_settings}), encoding="utf-8"
    )
    (out_dir / f"{stem}.cdi3.json").write_text(json.dumps({"Version": 3}), encoding="utf-8")
    (out_dir / "exp.json").write_text(json.dumps({"Type": "Live2D Expression", "Parameters": []}), encoding="utf-8")
    (out_dir / "idle.motion3.json").write_text(json.dumps({"Version": 3, "Curves": []}), encoding="utf-8")
    if groups is None:
        groups = [
            {"Target": "Parameter", "Name": "EyeBlink", "Ids": ["ParamEyeLOpen", "ParamEyeROpen"]},
            {"Target": "Parameter", "Name": "LipSync", "Ids": ["ParamMouthOpenY"]},
        ]
    (out_dir / f"{stem}.model3.json").write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": f"{stem}.moc3",
                    "Textures": [f"{stem}.4096/texture_00.png"],
                    "Physics": f"{stem}.physics3.json",
                    "DisplayInfo": f"{stem}.cdi3.json",
                    "Expressions": [{"Name": "Smile", "File": "exp.json"}],
                    "Motions": {"Idle": [{"File": "idle.motion3.json"}]},
                },
                "Groups": groups,
            }
        ),
        encoding="utf-8",
    )
    if vtube:
        (out_dir / f"{stem}.vtube.json").write_text(json.dumps({"Version": 1}), encoding="utf-8")


# ---------- Math & image reprojection helpers ----------


def test_unpad_params_landscape_portrait_and_invalid():
    scale, px, py = lp.unpad_params(2000, 1000, 1000)
    assert scale == pytest.approx(0.5)
    assert px == 0.0
    assert py == 250.0

    scale2, px2, py2 = lp.unpad_params(1000, 2000, 1000)
    assert scale2 == pytest.approx(0.5)
    assert px2 == 250.0
    assert py2 == 0.0

    with pytest.raises(ValueError, match="positive"):
        lp.unpad_params(0, 100, 1280)
    with pytest.raises(ValueError, match="positive"):
        lp.unpad_params(100, -1, 1280)
    with pytest.raises(ValueError, match="positive"):
        lp.unpad_params(100, 100, 0)


def test_map_bbox():
    rect = lp.map_bbox((100, 200, 300, 500), 0.5, 50.0, 100.0, off_x=10.0, off_y=20.0)
    assert rect == {"left": 110.0, "top": 220.0, "width": 400.0, "height": 600.0}


def test_paste_onto_canvas_clips_and_rejects_empty():
    im = Image.new("RGBA", (20, 20), (10, 20, 30, 255))
    arr, box = lp.paste_onto_canvas(
        im, {"left": -5.0, "top": 5.0, "width": 20.0, "height": 20.0}, cw=20, ch=20
    )
    assert box == (0, 5, 15, 20)
    assert arr.shape == (15, 15, 4)
    assert tuple(arr[0, 0]) == (10, 20, 30, 255)

    with pytest.raises(ValueError, match="outside canvas"):
        lp.paste_onto_canvas(
            im, {"left": 100.0, "top": 100.0, "width": 10.0, "height": 10.0}, cw=20, ch=20
        )


def test_reproject_visible_pixels_replaces_unoccluded_and_preserves_occluded():
    orig = np.zeros((40, 40, 4), dtype=np.uint8)
    orig[:, :] = (200, 50, 10, 255)

    layer = np.zeros((40, 40, 4), dtype=np.uint8)
    layer[:, :] = (20, 40, 220, 200)

    occluder = np.zeros((40, 40), dtype=bool)
    occluder[0:20, :] = True  # top half occluded

    out = lp.reproject_visible_pixels(
        layer, (0, 0, 40, 40), orig, occluder, dilate_iters=1, feather_sigma=0.5
    )
    assert out[2, 20, 2] > 180
    assert out[35, 20, 0] > 180
    assert out[35, 20, 3] == 255


def test_split_eyes_and_brows_front_three_quarter_and_warnings(tmp_path: Path):
    # Front-facing eyes + tiny noise speck
    img = np.zeros((100, 200, 4), dtype=np.uint8)
    img[10:18, 20:60] = (40, 30, 30, 255)  # brow_r
    img[45:80, 25:75] = (80, 90, 200, 255)  # eye_r
    img[10:18, 140:180] = (40, 30, 30, 255)  # brow_l
    img[45:80, 125:175] = (80, 90, 200, 255)  # eye_l
    img[95, 95:97] = (255, 255, 255, 255)  # tiny speck skipped

    src = tmp_path / "eyes.png"
    Image.fromarray(img).save(src)
    out_parts = tmp_path / "parts"
    warns: list[str] = []

    res = lp.split_eyes_and_brows(
        src,
        out_parts,
        "char_eye",
        {"left": 100.0, "top": 200.0, "width": 400.0, "height": 200.0},
        warnings=warns,
    )
    assert set(res.keys()) == {"eye_r", "eye_l", "brow_r", "brow_l"}
    assert warns == []

    # F5: Three-quarter view where character-left brow sits lower than 0.36*h (y=40..48)
    tq = np.zeros((100, 200, 4), dtype=np.uint8)
    tq[12:20, 20:60] = (40, 30, 30, 255)  # brow_r (high)
    tq[45:80, 25:75] = (80, 90, 200, 255)  # eye_r
    tq[40:48, 140:180] = (40, 30, 30, 255)  # brow_l (y_max=47 > 36!)
    tq[58:90, 125:175] = (80, 90, 200, 255)  # eye_l
    tq_src = tmp_path / "eyes_3q.png"
    Image.fromarray(tq).save(tq_src)
    warns_tq: list[str] = []
    res_tq = lp.split_eyes_and_brows(
        tq_src, out_parts, "tq", {"left": 0.0, "top": 0.0, "width": 200.0, "height": 100.0}, warnings=warns_tq
    )
    assert set(res_tq.keys()) == {"eye_r", "eye_l", "brow_r", "brow_l"}
    assert res_tq["brow_l"][2]["top"] == 40.0
    assert warns_tq == []

    # F5: Missing brow on left side emits a warning instead of silent merge
    no_brow_l = np.zeros((100, 200, 4), dtype=np.uint8)
    no_brow_l[10:18, 20:60] = (40, 30, 30, 255)
    no_brow_l[45:80, 25:75] = (80, 90, 200, 255)
    no_brow_l[45:80, 125:175] = (80, 90, 200, 255)  # only eye on side 'l'
    nbl_src = tmp_path / "no_brow_l.png"
    Image.fromarray(no_brow_l).save(nbl_src)
    warns_nbl: list[str] = []
    res_nbl = lp.split_eyes_and_brows(
        nbl_src, out_parts, "nbl", {"left": 0.0, "top": 0.0, "width": 200.0, "height": 100.0}, warnings=warns_nbl
    )
    assert "brow_l" not in res_nbl and "eye_l" in res_nbl
    assert any("side 'l'" in w for w in warns_nbl)

    # Side with two components where upper component is too tall to be a brow -> both stay eye + warn
    tall_upper = np.zeros((100, 200, 4), dtype=np.uint8)
    tall_upper[5:45, 20:60] = (80, 90, 200, 255)  # h=40 > 0.30*100
    tall_upper[60:95, 20:60] = (80, 90, 200, 255)
    tu_src = tmp_path / "tall_upper.png"
    Image.fromarray(tall_upper).save(tu_src)
    warns_tu: list[str] = []
    res_tu = lp.split_eyes_and_brows(
        tu_src, out_parts, "tu", {"left": 0.0, "top": 0.0, "width": 200.0, "height": 100.0}, warnings=warns_tu
    )
    assert "brow_r" not in res_tu and "eye_r" in res_tu
    assert any("side 'r'" in w for w in warns_tu)

    # r1-1: Lower lash below iris must NOT steal eye_anchor or push upper lash/iris into brow
    multi_eye = np.zeros((100, 200, 4), dtype=np.uint8)
    multi_eye[10:17, 20:65] = (40, 30, 30, 255)  # brow (y=10..16)
    multi_eye[38:49, 20:75] = (20, 20, 20, 255)  # upper lash (y=38..48)
    multi_eye[50:75, 30:65] = (80, 90, 200, 255)  # iris (y=50..74, h=25<=30)
    multi_eye[78:83, 25:60] = (30, 30, 30, 255)  # lower lash (y=78..82)
    me_src = tmp_path / "multi_eye.png"
    Image.fromarray(multi_eye).save(me_src)
    warns_me: list[str] = []
    res_me = lp.split_eyes_and_brows(
        me_src, out_parts, "me", {"left": 0.0, "top": 0.0, "width": 200.0, "height": 100.0}, warnings=warns_me
    )
    assert res_me["brow_r"][2]["top"] == 10.0 and res_me["brow_r"][2]["height"] == 7.0
    assert res_me["eye_r"][2]["top"] == 38.0 and res_me["eye_r"][2]["height"] == 45.0
    assert warns_me == []

    # r2-3: Horizontally split brow (by bangs) stays together in brow_r; browless upper lash stays in eye_r + warns
    split_brow = np.zeros((100, 200, 4), dtype=np.uint8)
    split_brow[10:18, 15:35] = (40, 30, 30, 255)  # brow fragment 1
    split_brow[11:19, 45:65] = (40, 30, 30, 255)  # brow fragment 2
    split_brow[45:80, 20:70] = (80, 90, 200, 255)  # eye_r
    sb_src = tmp_path / "split_brow.png"
    Image.fromarray(split_brow).save(sb_src)
    warns_sb: list[str] = []
    res_sb = lp.split_eyes_and_brows(
        sb_src, out_parts, "sb", {"left": 0.0, "top": 0.0, "width": 200.0, "height": 100.0}, warnings=warns_sb
    )
    assert res_sb["brow_r"][2]["width"] == 50.0  # spans x=15..65 (both brow fragments!)
    assert res_sb["eye_r"][2]["top"] == 45.0
    assert warns_sb == []

    browless_lash = np.zeros((100, 200, 4), dtype=np.uint8)
    browless_lash[38:49, 20:70] = (20, 20, 20, 255)  # upper lash (y_max=48)
    browless_lash[50:75, 30:65] = (80, 90, 200, 255)  # iris (y_min=50 -> gap=2 < 4)
    bl_src = tmp_path / "browless_lash.png"
    Image.fromarray(browless_lash).save(bl_src)
    warns_bl: list[str] = []
    res_bl = lp.split_eyes_and_brows(
        bl_src, out_parts, "bl", {"left": 0.0, "top": 0.0, "width": 200.0, "height": 100.0}, warnings=warns_bl
    )
    assert "brow_r" not in res_bl and res_bl["eye_r"][2]["top"] == 38.0
    assert any("side 'r'" in w for w in warns_bl)

    # Real-sprite edge cases (s12):
    # 1) Curved/slanted brow whose outer tip dips below eye.y_min (bbox Y overlaps, per-column gap >= 4)
    # 2) Tiny 3x5 dust speck above real brow must NOT steal primary brow seed
    # 3) Horizontally disjoint brow (no shared X columns with eye) falls back to bbox Y gap
    curved = np.zeros((120, 220, 4), dtype=np.uint8)
    # Side 'r': curved brow y=10..18 on x=20..52 dipping to y=17..35 on x=49..62; eye y=26..90 on x=20..48
    curved[10:18, 20:52] = (40, 30, 30, 255)
    curved[17:35, 49:62] = (40, 30, 30, 255)  # 4-connected slanted brow tail dipping below eye.y_min=26!
    curved[26:90, 20:48] = (80, 90, 200, 255)  # eye_r (area=1792)
    curved[1:5, 30:34] = (40, 30, 30, 255)  # 16px dust speck (< 1.5% of eye area) above brow
    # Side 'l': brow horizontally offset from eye (no shared X columns)
    curved[10:18, 120:150] = (40, 30, 30, 255)
    curved[45:90, 160:200] = (80, 90, 200, 255)
    curved_src = tmp_path / "curved.png"
    Image.fromarray(curved).save(curved_src)
    warns_cv: list[str] = []
    res_cv = lp.split_eyes_and_brows(
        curved_src, out_parts, "cv", {"left": 0.0, "top": 0.0, "width": 220.0, "height": 120.0}, warnings=warns_cv
    )
    assert set(res_cv.keys()) == {"eye_r", "eye_l", "brow_r", "brow_l"}
    assert res_cv["brow_r"][2]["top"] == 10.0 and res_cv["brow_r"][2]["height"] == 25.0
    assert res_cv["brow_l"][2]["top"] == 10.0
    assert warns_cv == []

    # Missing brow with warnings=None works cleanly without crashing
    res_nbl_nowarn = lp.split_eyes_and_brows(
        nbl_src, out_parts, "nbl2", {"left": 0.0, "top": 0.0, "width": 200.0, "height": 100.0}
    )
    assert "brow_l" not in res_nbl_nowarn

    # Empty image produces empty dict
    empty_src = tmp_path / "empty.png"
    Image.new("RGBA", (60, 60), (0, 0, 0, 0)).save(empty_src)
    assert lp.split_eyes_and_brows(
        empty_src, out_parts, "empty", {"left": 0.0, "top": 0.0, "width": 60.0, "height": 60.0}
    ) == {}


# ---------- Single-image alpha-residual recovery (Case 2B) ----------


class FakePSDLayer:
    def __init__(self, name: str, bbox: tuple[int, int, int, int], img: Image.Image | None):
        self.name = name
        self.bbox = bbox
        self._img = img

    def composite(self) -> Image.Image | None:
        return self._img


class FakePSD:
    def __init__(self, width: int, layers: list[FakePSDLayer]):
        self.width = width
        self._layers = layers

    def __iter__(self):
        return iter(self._layers)


def test_build_single_image_layout_native_resolution_and_recovery(tmp_path: Path):
    # F6: Portrait high-res source (300x600) with lower-res See-through PSD (200x200)
    # Must preserve native 300x600 canvas and native-pixel layer dimensions!
    ow, oh, res = 300, 600, 200
    orig = np.zeros((oh, ow, 4), dtype=np.uint8)
    orig[10:90, 90:210] = (30, 20, 60, 255)  # hat in top 22%
    orig[120:450, 75:225] = (240, 210, 190, 255)  # body/face
    orig[500:590, 90:210] = (50, 30, 20, 255)  # shoes in bottom 22%
    orig[4:6, 4:6] = (255, 0, 0, 255)  # tiny speck below min_top_px

    src_rgba = tmp_path / "char_hi.png"
    Image.fromarray(orig).save(src_rgba)

    # In 200x200 square space: scale = 200/600 = 1/3, nw = 100, nh = 200, pad_x = 50, pad_y = 0
    # Body x=75..225 -> 50+25..50+75 = 75..125; y=120..450 -> 40..150
    leg_img = Image.new("RGBA", (50, 50), (100, 100, 120, 255))
    face_img = Image.new("RGBA", (50, 60), (200, 180, 170, 255))
    hw_img = Image.new("RGBA", (30, 10), (180, 40, 40, 255))
    extra_img = Image.new("RGBA", (10, 10), (10, 200, 10, 255))
    fake_psd = FakePSD(
        width=res,
        layers=[
            FakePSDLayer("empty_layer", (0, 0, 10, 10), None),
            FakePSDLayer("legwear", (75, 100, 125, 150), leg_img),
            FakePSDLayer("face", (75, 40, 125, 100), face_img),
            FakePSDLayer("custom_unmapped", (90, 60, 100, 70), extra_img),
            FakePSDLayer("headwear", (85, 30, 115, 40), hw_img),
        ],
    )

    out_dir = tmp_path / "case2b"
    manifest = lp.build_single_image_layout(
        src_rgba,
        tmp_path / "fake.psd",
        out_dir,
        name="demo",
        psd_loader=lambda _p: fake_psd,
        reproject=True,
    )
    assert manifest["format"] == "psd2live-layout"
    assert manifest["canvas"] == {"width": ow, "height": oh}
    face_layer = next(l for l in manifest["layers"] if l["name"] == "face")
    # Mapped from 50x60 in 200px space back to 150x180 in native 300x600 space!
    assert face_layer["image"]["width"] == 150
    assert face_layer["image"]["height"] == 180
    ids = [l["id"] for l in manifest["layers"]]
    assert "shoes_recovered" in ids
    assert "hat_recovered" in ids
    assert ids.index("hat_recovered") < ids.index("st_04_headwear")
    assert (out_dir / "layout.json").exists()

    # No-reproject, overhanging layer bbox, out-of-bounds layer, and unsafe layer names
    orig2 = np.zeros((200, 200, 4), dtype=np.uint8)
    orig2[40:100, 60:140] = (240, 210, 190, 255)
    src2 = tmp_path / "char2.png"
    Image.fromarray(orig2).save(src2)
    overhang_img = Image.new("RGBA", (20, 20), (10, 20, 30, 255))
    fake_psd2 = FakePSD(
        width=200,
        layers=[
            FakePSDLayer("/../../escaped", (60, 40, 140, 100), Image.new("RGBA", (80, 60), (200, 180, 170, 255))),
            FakePSDLayer("hair/front:L", (-5, 10, 15, 30), overhang_img),
            FakePSDLayer("///", (190, 10, 210, 30), overhang_img),
            FakePSDLayer("fully_outside", (250, 250, 260, 260), overhang_img),
        ],
    )
    out2 = tmp_path / "case2b_plain"
    m2 = lp.build_single_image_layout(
        src2,
        tmp_path / "fake2.psd",
        out2,
        name="plain",
        psd_loader=lambda _p: fake_psd2,
        reproject=False,
    )
    ids2 = [l["id"] for l in m2["layers"]]
    assert ids2 == ["st_00_escaped", "st_01_hair_front_L", "st_02_layer"]
    assert "shoes_recovered" not in ids2
    assert "hat_recovered" not in ids2
    for lay in m2["layers"]:
        part_file = (out2 / lay["image"]["path"]).resolve()
        assert part_file.parent == (out2 / "parts").resolve()
        assert part_file.exists()
    assert m2["layers"][1]["rect"] == {"left": 0.0, "top": 10.0, "width": 15.0, "height": 20.0}
    assert m2["layers"][2]["rect"] == {"left": 190.0, "top": 10.0, "width": 10.0, "height": 20.0}

    # Landscape image (400x200) keeps native 400x200 canvas and recovers hat/shoes
    land = np.zeros((200, 400, 4), dtype=np.uint8)
    land[4:36, 140:260] = (30, 20, 60, 255)
    land[50:150, 120:280] = (240, 210, 190, 255)
    land[164:196, 140:260] = (50, 30, 20, 255)
    src_land = tmp_path / "land.png"
    Image.fromarray(land).save(src_land)
    land_body = Image.new("RGBA", (80, 50), (240, 210, 190, 255))
    fake_land_psd = FakePSD(width=200, layers=[FakePSDLayer("face", (60, 75, 140, 125), land_body)])
    m_land = lp.build_single_image_layout(
        src_land,
        tmp_path / "land.psd",
        tmp_path / "case2b_land",
        name="land",
        psd_loader=lambda _p: fake_land_psd,
        reproject=False,
    )
    assert m_land["canvas"] == {"width": 400, "height": 200}
    land_ids = [l["id"] for l in m_land["layers"]]
    assert "hat_recovered" in land_ids
    assert "shoes_recovered" in land_ids


# ---------- Web viewer patching & MOC3 validation ----------


def test_patch_web_viewer_escapes_html_script_and_comments(tmp_path: Path):
    index = tmp_path / "web" / "index.html"
    cfg = {
        "title": "Test <Model> & Co",
        "prop_expression_ids": ["exp_hat_off"],
        "toggles": [
            {
                "id": "hat_off",
                "label": "<!--<script></script><img onerror=1>-->",
                "parameter": "ParamHat",
                "on_value": 0.0,
                "off_value": 1.0,
            }
        ],
    }
    lp.patch_web_viewer(index, cfg)
    text = index.read_text(encoding="utf-8")
    assert "<title>Test &lt;Model&gt; &amp; Co</title>" in text
    # F11: Neither <!--<script>, </script>, nor --> may appear raw inside inline JSON
    assert "<!--<script>" not in text
    assert "</script><img" not in text
    assert "\\u003c!--\\u003cscript\\u003e" in text
    assert "P2LPlayer.create" in text
    assert "p2lReady" in text


def test_validate_moc3_dir_strict_checks(tmp_path: Path):
    # Missing directory
    missing = lp.validate_moc3_dir(tmp_path / "nope")
    assert missing["ok"] is False
    assert missing["error"] == "directory missing"

    # Empty directory
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    empty_res = lp.validate_moc3_dir(empty_dir)
    assert empty_res["ok"] is False
    assert empty_res["moc3"] is None
    assert empty_res["model3"] is None

    # F2: 5-byte fake MOC3\x05 with no model3.json must FAIL
    fake5_dir = tmp_path / "fake5"
    fake5_dir.mkdir()
    (fake5_dir / "fake.moc3").write_bytes(b"MOC3\x05")
    fake5_res = lp.validate_moc3_dir(fake5_dir)
    assert fake5_res["moc3"]["header_ok"] is False
    assert fake5_res["ok"] is False

    # Truncated <5 bytes header
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "tiny.moc3").write_bytes(b"MO")
    bad_res = lp.validate_moc3_dir(bad_dir)
    assert bad_res["moc3"]["header_ok"] is False
    assert bad_res["moc3"]["version"] is None
    assert bad_res["ok"] is False

    # Complete valid package (including matching physics3 outputs & VTS json)
    valid_dir = tmp_path / "valid"
    _write_valid_moc3_package(
        valid_dir,
        param_ids=["ParamAngleX", "ParamHairFront"],
        physics_outputs=["ParamHairFront"],
        vtube=True,
    )
    res = lp.validate_moc3_dir(valid_dir, require_vtube_json=True)
    assert res["ok"] is True
    assert res["moc3"]["header_ok"] is True
    assert res["moc3"]["version"] == 5
    assert res["moc3"]["big_endian"] is False
    assert res["model3"]["missing_files"] == []
    assert res["model3"]["missing_groups"] == []
    assert res["model3"]["empty_groups"] == []
    assert res["physics_unmatched_outputs"] == []
    assert res["vtube_json"] == "hero.vtube.json"
    assert res["warnings"] == []

    # Missing .vtube.json fails when require_vtube_json=True
    no_vts = tmp_path / "no_vts"
    _write_valid_moc3_package(no_vts, vtube=False)
    res_novts = lp.validate_moc3_dir(no_vts, require_vtube_json=True)
    assert res_novts["ok"] is False
    assert any("vtube.json" in i for i in res_novts["issues"])

    # Missing referenced files, traversal ref, missing/empty groups, unmatched physics output
    broken_dir = tmp_path / "broken"
    _write_valid_moc3_package(
        broken_dir,
        param_ids=["ParamAngleX"],
        physics_outputs=["ParamGhostPhysicsOut"],
        groups=[{"Name": "EyeBlink", "Ids": []}, "not_a_dict"],
    )
    (broken_dir / "hero.4096" / "texture_00.png").unlink()
    m3_path = broken_dir / "hero.model3.json"
    m3_data = json.loads(m3_path.read_text(encoding="utf-8"))
    m3_data["FileReferences"]["DisplayInfo"] = "../escaped.cdi3.json"
    m3_data["FileReferences"]["Expressions"].append({"Name": "NoFile"})
    m3_data["FileReferences"]["Motions"]["Idle"].append({})
    m3_path.write_text(json.dumps(m3_data), encoding="utf-8")
    res_broken = lp.validate_moc3_dir(broken_dir)
    assert res_broken["ok"] is False
    assert "hero.4096/texture_00.png" in res_broken["model3"]["missing_files"]
    assert "../escaped.cdi3.json" in res_broken["model3"]["missing_files"]
    assert res_broken["model3"]["missing_groups"] == ["LipSync"]
    assert res_broken["model3"]["empty_groups"] == ["EyeBlink"]
    assert res_broken["physics_unmatched_outputs"] == ["ParamGhostPhysicsOut"]
    assert any("physics3" in w for w in res_broken["warnings"])

    # Corrupt model3.json and corrupt physics3.json
    corrupt_m3 = tmp_path / "corrupt_m3"
    corrupt_m3.mkdir()
    (corrupt_m3 / "a.moc3").write_bytes(_make_valid_moc3_bytes())
    (corrupt_m3 / "a.model3.json").write_text("{bad json", encoding="utf-8")
    assert lp.validate_moc3_dir(corrupt_m3)["ok"] is False

    corrupt_phys = tmp_path / "corrupt_phys"
    _write_valid_moc3_package(corrupt_phys, param_ids=["ParamAngleX"])
    (corrupt_phys / "hero.physics3.json").write_text("{bad", encoding="utf-8")
    res_cp = lp.validate_moc3_dir(corrupt_phys)
    assert res_cp["ok"] is False
    assert any("physics3" in i for i in res_cp["issues"])

    # Missing Moc reference in model3.json & out-of-bounds moc3 param table fallback
    no_moc_ref = tmp_path / "no_moc_ref"
    _write_valid_moc3_package(no_moc_ref)
    # Overwrite moc3 with huge count_off so _extract_moc3_param_ids returns None safely
    raw_bad_off = bytearray(_make_valid_moc3_bytes(["ParamA"]))
    struct.pack_into("<I", raw_bad_off, 64, 9999999)
    (no_moc_ref / "hero.moc3").write_bytes(bytes(raw_bad_off))
    m3p = no_moc_ref / "hero.model3.json"
    d = json.loads(m3p.read_text(encoding="utf-8"))
    del d["FileReferences"]["Moc"]
    del d["FileReferences"]["Physics"]
    m3p.write_text(json.dumps(d), encoding="utf-8")
    res_nmr = lp.validate_moc3_dir(no_moc_ref)
    assert res_nmr["ok"] is False
    assert any("Moc" in i for i in res_nmr["issues"])

    # r1-3: Non-object JSON ([] / null) in model3.json and physics3.json must fail validation
    arr_m3 = tmp_path / "arr_m3"
    arr_m3.mkdir()
    (arr_m3 / "a.moc3").write_bytes(_make_valid_moc3_bytes())
    (arr_m3 / "a.model3.json").write_text("[]", encoding="utf-8")
    res_arr_m3 = lp.validate_moc3_dir(arr_m3)
    assert res_arr_m3["ok"] is False
    assert any("expected JSON object" in i for i in res_arr_m3["issues"])

    null_phys = tmp_path / "null_phys"
    _write_valid_moc3_package(null_phys, param_ids=["ParamAngleX"])
    (null_phys / "hero.physics3.json").write_text("null", encoding="utf-8")
    res_null_phys = lp.validate_moc3_dir(null_phys)
    assert res_null_phys["ok"] is False
    assert any("expected JSON object" in i for i in res_null_phys["issues"])

    # r1-2: physics3.json Output entry omitting Destination/Id must NOT raise KeyError
    bad_phys_out = tmp_path / "bad_phys_out"
    _write_valid_moc3_package(bad_phys_out, param_ids=["ParamAngleX"])
    (bad_phys_out / "hero.physics3.json").write_text(
        json.dumps({"Version": 3, "PhysicsSettings": [{"Output": [{}]}, "not_a_dict"]}),
        encoding="utf-8",
    )
    res_bpo = lp.validate_moc3_dir(bad_phys_out)
    assert res_bpo["ok"] is False
    assert any("Destination.Id" in i for i in res_bpo["issues"])

    # r2-1: JSON null nested fields in model3.json and physics3.json must NOT raise TypeError/AttributeError
    null_fields = tmp_path / "null_fields"
    null_fields.mkdir()
    (null_fields / "a.moc3").write_bytes(_make_valid_moc3_bytes(["ParamAngleX"]))
    (null_fields / "a.physics3.json").write_text(
        json.dumps({"Version": 3, "PhysicsSettings": [{"Output": None}]}), encoding="utf-8"
    )
    (null_fields / "a.model3.json").write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": "a.moc3",
                    "Textures": None,
                    "Physics": "a.physics3.json",
                    "Expressions": None,
                    "Motions": None,
                },
                "Groups": None,
            }
        ),
        encoding="utf-8",
    )
    res_nf = lp.validate_moc3_dir(null_fields)
    assert res_nf["ok"] is True
    assert res_nf["model3"]["textures"] == []
    assert res_nf["model3"]["expressions"] == []
    assert res_nf["model3"]["motions"] == {}

    null_refs = tmp_path / "null_refs"
    null_refs.mkdir()
    (null_refs / "a.moc3").write_bytes(_make_valid_moc3_bytes())
    (null_refs / "a.model3.json").write_text(
        json.dumps({"Version": 3, "FileReferences": None, "Groups": None}), encoding="utf-8"
    )
    assert lp.validate_moc3_dir(null_refs)["ok"] is False

    # r1-8: Big-endian MOC3 (raw[5] == 1) unpacks parameter table using >I
    be_buf = bytearray(512 + 64)
    be_buf[0:4] = b"MOC3"
    be_buf[4] = 5
    be_buf[5] = 1
    struct.pack_into(">I", be_buf, 64, 384)
    struct.pack_into(">I", be_buf, 64 + 50 * 4, 512)
    struct.pack_into(">6I", be_buf, 384, 1, 1, 1, 0, 1, 1)
    be_buf[512 : 512 + 11] = b"ParamBigEnd"
    assert lp._extract_moc3_param_ids(bytes(be_buf)) == {"ParamBigEnd"}

    # Also test _extract_moc3_param_ids when p_off is out of bounds
    raw_bad_poff = bytearray(_make_valid_moc3_bytes(["ParamA"]))
    struct.pack_into("<I", raw_bad_poff, 64 + 50 * 4, 9999999)
    assert lp._extract_moc3_param_ids(bytes(raw_bad_poff)) is None


def test_summarize_export_losses():
    fixture = json.loads((FIXTURES_DIR / "p2l_contract_fixture.json").read_text(encoding="utf-8"))
    summary = lp.summarize_export_losses(
        {
            "moc3": {"ok": True, "result": fixture["export_moc3_with_losses"]},
            "gif": {"ok": False, "error": "boom"},
            "web": {"ok": True, "result": {"exported": "web"}},
        }
    )
    assert summary["total_dropped"] == 1
    assert summary["total_approximated"] == 1
    assert summary["by_target"]["moc3"]["dropped"] == 1
    assert summary["by_target"]["moc3"]["approximated"] == 1
    assert "gif" not in summary["by_target"]
    assert summary["by_target"]["web"]["dropped"] == 0


# ---------- F1/F7: PSD2Live discovery (Win/macOS/Linux), command builder & wait_for_mcp ----------


def test_discover_p2l_dir_windows_macos_linux_and_errors(tmp_path: Path):
    # Windows/Linux portable
    p2l = tmp_path / "psd2live"
    (p2l / "app").mkdir(parents=True)
    assert lp.discover_p2l_dir(explicit=p2l) == p2l.resolve()
    assert lp.discover_p2l_dir(explicit=None, env={"PSD2LIVE_DIR": str(p2l)}) == p2l.resolve()

    # F1: macOS PSD2Live.app bundle (auto-resolves to Contents) and direct Contents path
    mac_app = tmp_path / "PSD2Live.app"
    (mac_app / "Contents" / "app").mkdir(parents=True)
    assert lp.discover_p2l_dir(explicit=mac_app) == (mac_app / "Contents").resolve()
    assert lp.discover_p2l_dir(explicit=mac_app / "Contents") == (mac_app / "Contents").resolve()

    with pytest.raises(FileNotFoundError, match="PSD2LIVE_DIR"):
        lp.discover_p2l_dir(explicit=None, env={})

    no_app = tmp_path / "incomplete"
    no_app.mkdir()
    with pytest.raises(FileNotFoundError, match="app"):
        lp.discover_p2l_dir(explicit=no_app)


def test_find_mcp_proxy_dir_layouts(tmp_path: Path):
    # Root layout (Windows/Linux portable)
    win = tmp_path / "win"
    win.mkdir()
    (win / "mcp_proxy.py").write_text("", encoding="utf-8")
    assert lp.find_mcp_proxy_dir(win) == win

    # macOS Contents/app/resources layout
    mac = tmp_path / "mac"
    (mac / "app" / "resources").mkdir(parents=True)
    (mac / "app" / "resources" / "mcp_proxy.py").write_text("", encoding="utf-8")
    assert lp.find_mcp_proxy_dir(mac) == mac / "app" / "resources"

    # tools/ layout
    tools = tmp_path / "tools_layout"
    (tools / "tools").mkdir(parents=True)
    (tools / "tools" / "mcp_proxy.py").write_text("", encoding="utf-8")
    assert lp.find_mcp_proxy_dir(tools) == tools / "tools"

    # Missing
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="mcp_proxy.py"):
        lp.find_mcp_proxy_dir(empty)


def test_build_p2l_mcp_cmd_java_variants_all_os(tmp_path: Path):
    store = tmp_path / "store"

    # Fallback to PATH "java"
    p_fallback = tmp_path / "fallback"
    (p_fallback / "app").mkdir(parents=True)
    assert lp.build_p2l_mcp_cmd(p_fallback, 23881, "tok", store)[0] == "java"

    # F1: macOS bundle runtime/Contents/Home/bin/java
    p_mac = tmp_path / "mac"
    (p_mac / "app").mkdir(parents=True)
    mac_java = p_mac / "runtime" / "Contents" / "Home" / "bin" / "java"
    mac_java.parent.mkdir(parents=True)
    mac_java.write_text("", encoding="utf-8")
    assert lp.build_p2l_mcp_cmd(p_mac, 23881, "tok", store)[0] == str(mac_java)

    # Linux/POSIX portable runtime/bin/java
    p_linux = tmp_path / "linux"
    (p_linux / "app").mkdir(parents=True)
    posix_java = p_linux / "runtime" / "bin" / "java"
    posix_java.parent.mkdir(parents=True)
    posix_java.write_text("", encoding="utf-8")
    assert lp.build_p2l_mcp_cmd(p_linux, 23881, "tok", store)[0] == str(posix_java)

    # Windows portable runtime/bin/java.exe
    win_java = p_linux / "runtime" / "bin" / "java.exe"
    win_java.write_text("", encoding="utf-8")
    assert lp.build_p2l_mcp_cmd(p_linux, 23881, "tok", store)[0] == str(win_java)


def test_pick_ephemeral_port_and_wait_for_mcp_auth_probe():
    port = lp.pick_ephemeral_port()
    assert isinstance(port, int) and port > 0
    # r1-6: Default opener omits ProxyHandler so localhost bearer token is never sent to HTTP_PROXY
    proxy_handlers = [
        h for h in lp._NO_PROXY_OPENER.__self__.handlers if isinstance(h, lp.urllib.request.ProxyHandler)
    ]
    assert proxy_handlers == []

    class AliveProc:
        def poll(self):
            return None

    class DeadProc:
        returncode = 7

        def poll(self):
            return 7

    class FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # F7: Succeeds when authenticated JSON-RPC reply arrives (verifies Authorization header)
    seen_reqs = []

    def ok_opener(req, timeout=1.0):
        seen_reqs.append(req)
        return FakeResp(b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}')

    lp.wait_for_mcp(port, AliveProc(), timeout=5.0, token="sec_tok", opener=ok_opener)
    assert seen_reqs[0].get_header("Authorization") == "Bearer sec_tok"

    # F7: Rejects HTTP 401 from another process on the port (must NOT return early!)
    ticks_401 = iter([0.0, 0.5, 2.0])

    def raise_401(req, timeout=1.0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, BytesIO(b"nope"))

    with pytest.raises(TimeoutError, match="did not start"):
        lp.wait_for_mcp(
            port,
            AliveProc(),
            timeout=1.0,
            token="sec_tok",
            opener=raise_401,
            sleep_fn=lambda _s: None,
            time_fn=lambda: next(ticks_401),
        )

    # F7: Rejects HTTP 200 non-JSON-RPC response (e.g., unrelated HTTP server)
    ticks_html = iter([0.0, 0.5, 2.0])
    with pytest.raises(TimeoutError, match="did not start"):
        lp.wait_for_mcp(
            port,
            AliveProc(),
            timeout=1.0,
            token="sec_tok",
            opener=lambda _r, timeout=1.0: FakeResp(b"<html>not mcp</html>"),
            sleep_fn=lambda _s: None,
            time_fn=lambda: next(ticks_html),
        )

    # Early process exit
    with pytest.raises(RuntimeError, match="exited early"):
        lp.wait_for_mcp(port, DeadProc(), timeout=5.0, token="sec_tok")

    # Connection refused until timeout
    ticks_conn = iter([0.0, 0.5, 2.0])

    def fail_conn(_r, timeout=1.0):
        raise ConnectionRefusedError("refused")

    with pytest.raises(TimeoutError, match="did not start"):
        lp.wait_for_mcp(
            port,
            AliveProc(),
            timeout=1.0,
            token="sec_tok",
            opener=fail_conn,
            sleep_fn=lambda _s: None,
            time_fn=lambda: next(ticks_conn),
        )


# ---------- apply_post_rig, contract fixture (F8) & run_p2l_pipeline ----------


class FakeP2LClient:
    def __init__(
        self,
        *,
        fail_export: str | None = None,
        fail_axis: bool = False,
        canvas_style: str = "list",
        empty_renders: bool = False,
        export_losses: list[dict] | None = None,
    ):
        self.edits: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail_export = fail_export
        self.fail_axis = fail_axis
        self.canvas_style = canvas_style
        self.empty_renders = empty_renders
        self.export_losses = export_losses or []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def call(self, tool: str, args: dict):
        self.calls.append((tool, args))
        if tool == "modifier_stack":
            return {
                "entries": [
                    {"id": "m1", "type": "mouth.outline", "config": {}},
                    {"id": "m2", "type": "face/fitted", "config": {"strength": 1.0, "tuning": {"a": 1}}},
                ]
            }, []
        if tool == "workspace_overview":
            canvas: object = [1000, 2000]
            if self.canvas_style == "dict":
                canvas = {"width": 1200, "height": 2400}
            elif self.canvas_style == "other":
                canvas = "invalid"
            return {
                "canvas": canvas,
                "parameters": {"items": [{"id": "ParamAngleX"}, {"id": "ParamHat"}]},
                "deformers": {
                    "items": [
                        {"name": "Head", "target": "warp:d_head"},
                        {"name": "RawDef", "target": "d_raw"},
                    ]
                },
            }, []
        if tool == "workspace_inspect":
            scope = args.get("scope")
            if scope == "objects":
                offset = args.get("offset", 0)
                if offset == 0:
                    items = [{"layerId": f"layer_{i}", "target": f"mesh:m_{i}"} for i in range(64)]
                    items[0] = {"layerId": "eye_close_l", "target": "mesh:m_eye_l"}
                    items[1] = {"layerId": "hat_outer", "target": "m_hat_outer"}
                    return {"items": items, "total": 65}, []
                return {"items": [{"layerId": "ribbon_r", "target": "mesh:m_rib_r"}], "total": 65}, []
            return {"ok": True}, []
        if tool == "motion_list":
            return {"clips": [{"id": "c1", "name": "Idle"}, {"id": "c2", "name": "Other"}]}, []
        if tool == "motion_get":
            return {
                "clip": {
                    "id": "c1",
                    "name": "Idle",
                    "loop": True,
                    "curves": [
                        {
                            "parameter": "ParamAngleX",
                            "keys": [
                                {"t": 0.0, "value": 40.0, "in": [0.0, 40.0], "out": [0.1, -40.0]},
                                {"t": 1.0, "value": -10.0},
                            ],
                        },
                        {"parameter": "ParamUnscaled", "keys": [{"t": 0.0, "value": 1.0}]},
                    ],
                }
            }, []
        if tool == "view_render_poses":
            return {"ok": True}, ([] if self.empty_renders else [b"\x89PNG\r\n\x1a\nfake"])
        return {}, []

    def edit(self, tool: str, **kwargs):
        self.edits.append((tool, kwargs))
        if tool == "author_axis" and self.fail_axis:
            raise RuntimeError("axis rejected")
        if tool == "rig_create_warp":
            return {"target": f"warp:{kwargs['id']}"}
        if tool == "canvas_warp":
            return {"id": kwargs["id"]}
        if tool == "project_export_target":
            if kwargs["target"] == self.fail_export:
                raise RuntimeError("export boom")
            out_dir = Path(kwargs["output_directory"])
            if kwargs["target"] in ("moc3", "vtube-studio"):
                _write_valid_moc3_package(out_dir, stem="demo", vtube=(kwargs["target"] == "vtube-studio"))
            if kwargs["target"] == "web":
                (out_dir / "index.html").write_text("<html>old</html>", encoding="utf-8")
            res: dict = {"exported": kwargs["target"]}
            if kwargs["target"] in ("moc3", "vtube-studio") and self.export_losses:
                res["losses"] = self.export_losses
            return res
        return {"ok": True}


def test_recorded_mcp_contract_fixture(tmp_path: Path):
    """F8: Replay recorded PSD2Live 3.8.4 MCP response shapes through apply_post_rig + run_p2l_pipeline."""
    fixture = json.loads((FIXTURES_DIR / "p2l_contract_fixture.json").read_text(encoding="utf-8"))

    class RecordedFixtureClient:
        def __init__(self):
            self.edits: list[tuple[str, dict]] = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, tool: str, args: dict):
            if tool == "modifier_stack":
                return fixture["modifier_stack"], []
            if tool == "workspace_overview":
                return fixture["workspace_overview"], []
            if tool == "workspace_inspect":
                scope = args.get("scope")
                return fixture[f"workspace_inspect_{scope}"], []
            if tool == "motion_list":
                return fixture["motion_list"], []
            if tool == "motion_get":
                return fixture["motion_get_idle"], []
            if tool == "view_render_poses":
                return {"ok": True}, [b"\x89PNG\r\n\x1a\n"]
            return {}, []

        def edit(self, tool: str, **kwargs):
            self.edits.append((tool, kwargs))
            if tool == "project_export_target":
                out_dir = Path(kwargs["output_directory"])
                if kwargs["target"] in ("moc3", "vtube-studio"):
                    _write_valid_moc3_package(out_dir, stem="layout", vtube=(kwargs["target"] == "vtube-studio"))
                    return fixture["export_moc3_with_losses"]
            return {"ok": True}

    p2l_dir = tmp_path / "PSD2Live.app" / "Contents"
    (p2l_dir / "app").mkdir(parents=True)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text("{}", encoding="utf-8")
    post_rig_path = tmp_path / "post_rig.json"
    post_rig_path.write_text(
        json.dumps(
            {
                "disable_modifiers": ["mouth.outline"],
                "modifier_updates": {"face/fitted": {"strength": 0.46, "tuning": {"turn": 20}}},
                "bind_to_deformer": [{"deformer_name": "頭部コンテナ", "layer_ids": ["headdress_outer"]}],
                "motion_scale": {"clips": ["Idle"], "parameters": {"ParamAngleX": 0.65}},
                "parameter_defaults": {"ParamAngleX": {"min": -25.0, "max": 25.0}},
            }
        ),
        encoding="utf-8",
    )

    class DummyProc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=5):
            pass

    client_box: list[RecordedFixtureClient] = []

    def make_client(_dir, _ep, _tok):
        c = RecordedFixtureClient()
        client_box.append(c)
        return c

    report = lp.run_p2l_pipeline(
        out_dir=tmp_path / "out_fixture",
        name="fixture_char",
        p2l_dir=p2l_dir,
        layout_path=layout_path,
        post_rig_path=post_rig_path,
        popen_fn=lambda *a, **kw: DummyProc(),
        wait_fn=lambda *a, **kw: None,
        client_factory=make_client,
    )
    assert report["post_rig_summary"]["rebound"] == 1
    assert report["post_rig_summary"]["warnings"] == []
    # Motion key 22.0 * 0.65 = 14.3 verified against recorded Idle clip
    motion_edit = next(kw for t, kw in client_box[0].edits if t == "motion_put")
    assert motion_edit["clip"]["curves"][0]["keys"][0]["value"] == pytest.approx(14.3)
    # F2: Export losses surfaced
    assert report["export_losses"]["total_dropped"] == 2  # moc3 + vtube-studio
    assert report["export_losses"]["total_approximated"] == 2
    assert report["moc3_validation"]["ok"] is True
    assert report["vts_validation"]["ok"] is True


def test_apply_post_rig_full_order_and_warnings():
    client = FakeP2LClient()
    recipe = {
        "settings_update": {"fps": 60},
        "disable_modifiers": ["mouth.outline"],
        "modifier_updates": {"face/fitted": {"strength": 0.5, "tuning": {"b": 2}}},
        "parameter_defaults": {
            "ParamHat": {"name": "Hat", "default": 1.0},
            "ParamAngleX": {"min": -25.0, "max": 25.0},
            "ParamMissing": {"default": 1.0},
        },
        "bind_to_deformer": [
            {"deformer_name": "Head", "layer_ids": ["hat_outer", "missing_layer"]},
            {"deformer_name": "RawDef", "layer_ids": ["ribbon_r"]},
            {"deformer_name": "NoSuchDef", "layer_ids": ["hat_outer"]},
        ],
        "create_warps": [
            {"id": "w_hat", "layer_ids": ["hat_outer", "missing_w"], "deformer_ids": ["Head", "missing_d"]},
            {"id": "w_empty", "layer_ids": ["missing_only"]},
        ],
        "author_axes": [
            {
                "parameter": {"id": "ParamHdYaw", "name": "Yaw", "min": -1, "max": 1, "default": 0},
                "shapes": [
                    {"target": "layer:hat_outer", "value": -1.0, "operations": []},
                    {"target": "layer:missing_ax", "value": 1.0, "operations": []},
                    {"target": "deformer:Head", "value": 1.0, "operations": []},
                    {"target": "deformer:MissingDef", "value": 1.0, "operations": []},
                    {"target": "warp:direct", "value": 1.0, "operations": []},
                ],
            },
            {
                "parameter": {"id": "ParamAngleX", "name": "AngleX", "min": -25, "max": 25, "default": 0},
                "shapes": [{"target": "layer:hat_outer", "value": -25.0, "operations": []}],
            },
            # r1-4: Non-clamped author_axis targeting a canvas_warp deformer runs in Phase B after canvas_warp
            {
                "parameter": {"id": "ParamCwAxis", "name": "CW Axis", "min": -1, "max": 1, "default": 0},
                "shapes": [{"target": "deformer:cw1", "value": 1.0, "operations": []}],
            },
        ],
        "keyform_applies": [
            {"op": "set", "target": "layer:eye_close_l", "key": {"ParamEyeLOpen": 0.0}, "channels": {"opacity": 1.0}},
            {"op": "set", "target": "layer:missing_kf", "key": {"ParamEyeLOpen": 0.0}, "channels": {"opacity": 0.0}},
        ],
        "motion_scale": {"clips": ["Idle"], "parameters": {"ParamAngleX": 0.65}},
        "canvas_warps": [
            {"id": "cw1", "layer_ids": ["hat_outer", "ribbon_r", "missing_cw"], "parent_deformer": "Head"},
            {"id": "cw2", "layer_ids": ["hat_outer"], "parent_deformer": "RawDef"},
            {"id": "cw_skip", "layer_ids": ["hat_outer"], "parent_deformer": "NoDef"},
            {"id": "cw_noparent", "layer_ids": ["hat_outer"]},
        ],
        "physics_modifiers": [
            {"id": "PhysicsHdYaw", "layer_ids": ["hat_outer", "ribbon_r", "missing_pm"], "inputs": []},
            {"id": "PhysicsNoTargets", "layer_ids": ["missing_only"], "inputs": []},
        ],
        "swings": [
            {"id": "sw1", "layer_id": "ribbon_r", "motions": [{"kind": "lateral"}]},
            {"id": "sw_raw", "layer_id": "hat_outer", "motions": [{"kind": "lateral"}]},
            {"id": "sw_miss", "layer_id": "nope", "motions": []},
        ],
    }
    exprs = [{"id": "exp_smile", "name": "Smile", "entries": []}]

    summary = lp.apply_post_rig(client, recipe, expressions=exprs)
    assert summary["rebound"] == 2
    assert summary["keyforms"] == 1
    assert summary["warnings"]
    axis_edits = [kw for t, kw in client.edits if t == "author_axis"]
    for ae in axis_edits:
        for sh in ae["shapes"]:
            assert not sh["target"].startswith(("layer:", "deformer:"))
    kf_edits = [kw for t, kw in client.edits if t == "keyform_apply"]
    assert len(kf_edits) == 1 and len(kf_edits[0]["changes"]) == 1

    tool_seq = [t for t, _ in client.edits]
    assert tool_seq.index("motion_put") < tool_seq.index("expression_put")
    assert tool_seq.index("expression_put") < tool_seq.index("canvas_warp")
    clamp_idx = next(i for i, (t, kw) in enumerate(client.edits) if t == "parameter_update" and "min" in kw)
    assert clamp_idx < tool_seq.index("canvas_warp")
    cw_axis_idx = next(
        i for i, (t, kw) in enumerate(client.edits) if t == "author_axis" and kw["parameter"]["id"] == "ParamCwAxis"
    )
    assert cw_axis_idx > tool_seq.index("canvas_warp")
    assert client.edits[cw_axis_idx][1]["shapes"][0]["target"] == "warp:cw1"

    skip_client = FakeP2LClient()
    lp.apply_post_rig(
        skip_client,
        {
            "author_axes": [
                {
                    "parameter": {"id": "ParamSkip"},
                    "shapes": [{"target": "layer:nonexistent", "value": 1.0, "operations": []}],
                }
            ]
        },
    )
    assert not any(t == "author_axis" for t, _ in skip_client.edits)

    bad_client = FakeP2LClient(fail_axis=True)
    with pytest.raises(RuntimeError, match="axis rejected"):
        lp.apply_post_rig(
            bad_client,
            {
                "author_axes": [
                    {
                        "parameter": {"id": "ParamTest"},
                        "shapes": [{"target": "warp:direct", "value": 1.0, "operations": []}],
                    }
                ]
            },
        )

    empty_client = FakeP2LClient()
    empty_summary = lp.apply_post_rig(
        empty_client,
        {"motion_scale": {"clips": ["Idle"], "parameters": {"ParamNonExistent": 0.5}}},
    )
    assert empty_summary["rebound"] == 0


def test_run_p2l_pipeline_ephemeral_port_redaction_extra_poses_and_modes(tmp_path: Path, monkeypatch):
    p2l_dir = tmp_path / "p2l"
    (p2l_dir / "app").mkdir(parents=True)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text("{}", encoding="utf-8")
    expr_path = tmp_path / "exprs.json"
    expr_path.write_text(json.dumps([{"id": "exp_1", "name": "E1", "entries": []}]), encoding="utf-8")
    post_rig_path = tmp_path / "post_rig.json"
    post_rig_path.write_text(json.dumps({"web_ui": {"title": "Hero", "toggles": []}}), encoding="utf-8")
    # F11: 4 extra poses + 6 defaults = 10 -> must APPEND (keep ParamMouthOpenY=1.0 & head turns!) and chunk into 2 pages
    extra_poses = tmp_path / "extra.json"
    extra_poses.write_text(json.dumps([{"ParamAngleX": 10.0}] * 4), encoding="utf-8")
    sabun_poses = tmp_path / "sabun.json"
    sabun_poses.write_text(
        json.dumps(
            {
                "viewport": {"left": 0, "top": 0, "width": 500, "height": 500},
                "columns": 3,
                "poses": [{}] * 9,
            }
        ),
        encoding="utf-8",
    )

    class DummyProc:
        def __init__(self, *, timeout_on_wait: bool = False):
            self._timeout = timeout_on_wait
            self.killed = False
            self.wait_calls = 0

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=5):
            self.wait_calls += 1
            if self._timeout and self.wait_calls == 1:
                raise lp.subprocess.TimeoutExpired(cmd="java", timeout=timeout)

        def kill(self):
            self.killed = True

    out_dir = tmp_path / "out_layout"
    (out_dir / "_mcp_store").mkdir(parents=True)

    # F10: Simulate PSD2Live printing bearer token into stdout & stderr logs; must be redacted!
    seen_tokens: list[str] = []
    seen_ports: list[int] = []
    clients: list[FakeP2LClient] = []

    def logging_popen(cmd, stdout=None, stderr=None, **kw):
        tok_idx = cmd.index("--token") + 1
        port_idx = cmd.index("--port") + 1
        tok = cmd[tok_idx]
        seen_tokens.append(tok)
        seen_ports.append(int(cmd[port_idx]))
        if stdout is not None:
            stdout.write(f"MCP listening with token={tok}\n")
        if stderr is not None:
            stderr.write(f"debug token {tok}\n")
        return DummyProc()

    def capture_client(_dir, _ep, _tok):
        c = FakeP2LClient(fail_export="gif", canvas_style="dict")
        clients.append(c)
        return c

    report = lp.run_p2l_pipeline(
        out_dir=out_dir,
        name="hero",
        p2l_dir=p2l_dir,
        layout_path=layout_path,
        expressions_path=expr_path,
        extra_poses_path=extra_poses,
        post_rig_path=post_rig_path,
        sabun_poses_path=sabun_poses,
        face_rect="10,20,300,400",
        popen_fn=logging_popen,
        wait_fn=lambda *a, **kw: None,
        client_factory=capture_client,
    )
    # F7: default port=0 picks a real ephemeral port > 0
    assert seen_ports[0] > 0
    # F10: bearer token redacted from logs on disk
    stdout_text = (out_dir / "_mcp_stdout.log").read_text(encoding="utf-8")
    stderr_text = (out_dir / "_mcp_stderr.log").read_text(encoding="utf-8")
    assert seen_tokens[0] not in stdout_text
    assert seen_tokens[0] not in stderr_text
    assert "<REDACTED>" in stdout_text
    assert "<REDACTED>" in stderr_text

    # F11: --extra-poses appends to all 6 defaults (mouth-open & head-turn preserved!) and chunks >9
    render_calls = [args for tool, args in clients[0].calls if tool == "view_render_poses"]
    face_poses_all = render_calls[1]["poses"] + render_calls[2]["poses"]
    assert len(face_poses_all) == 10
    assert {"ParamMouthOpenY": 1.0} in face_poses_all
    assert {"ParamAngleX": -25.0, "ParamAngleY": 10.0} in face_poses_all
    assert (out_dir / "poses_face_1.png").exists()
    assert (out_dir / "poses_face_2.png").exists()

    assert report["exports"]["moc3"]["ok"] is True
    assert report["exports"]["gif"]["ok"] is False
    assert (out_dir / "poses_full.png").exists()
    assert (out_dir / "poses_sabun_1.png").exists()
    assert (out_dir / "poses_sabun_2.png").exists()
    assert "P2LPlayer.create" in (out_dir / "export_web" / "index.html").read_text(encoding="utf-8")

    # PSD mode + macOS .app path directly (r1-7) + explicit port + kill+wait reap (r1-5)
    mac_app = tmp_path / "PSD2Live.app"
    (mac_app / "Contents" / "app").mkdir(parents=True)
    psd_path = tmp_path / "char.psd"
    psd_path.write_bytes(b"8BPS")
    sabun_one = tmp_path / "sabun1.json"
    sabun_one.write_text(
        json.dumps({"viewport": {"left": 0, "top": 0, "width": 100, "height": 100}, "poses": [{}]}),
        encoding="utf-8",
    )
    proc2 = DummyProc(timeout_on_wait=True)
    out_psd = tmp_path / "out_psd"
    seen_client_dirs: list[Path] = []
    report2 = lp.run_p2l_pipeline(
        out_dir=out_psd,
        name="psd_hero",
        p2l_dir=mac_app,
        psd_path=psd_path,
        port=24567,
        expressions_path=expr_path,
        sabun_poses_path=sabun_one,
        popen_fn=lambda *a, **kw: proc2,
        wait_fn=lambda *a, **kw: None,
        client_factory=lambda pdir, _ep, _tok: seen_client_dirs.append(pdir) or FakeP2LClient(canvas_style="other"),
    )
    assert seen_client_dirs == [(mac_app / "Contents").resolve()]
    assert proc2.killed is True and proc2.wait_calls == 2
    assert (out_psd / "poses_face.png").exists()
    assert (out_psd / "poses_sabun.png").exists()
    assert report2["exports"]["moc3"]["ok"] is True
    assert report2["moc3_validation"]["ok"] is True
    assert report2["vts_validation"]["ok"] is True

    # Validation errors upfront
    with pytest.raises(ValueError, match="exactly one"):
        lp.run_p2l_pipeline(out_dir=tmp_path / "x", name="x", p2l_dir=p2l_dir)
    with pytest.raises(FileNotFoundError, match="post_rig_path"):
        lp.run_p2l_pipeline(
            out_dir=tmp_path / "missing_cfg",
            name="x",
            p2l_dir=p2l_dir,
            layout_path=layout_path,
            post_rig_path=tmp_path / "does_not_exist.json",
        )
    popen_called = []
    with pytest.raises(ValueError, match="face_rect"):
        lp.run_p2l_pipeline(
            out_dir=tmp_path / "bad_rect",
            name="x",
            p2l_dir=p2l_dir,
            layout_path=layout_path,
            face_rect="10,20,300",
            popen_fn=lambda *a, **kw: popen_called.append(True),
        )
    assert popen_called == []

    # Stale export_moc3 cleanup + CREATE_NO_WINDOW truthy
    stale_out = tmp_path / "stale_out"
    (stale_out / "export_moc3").mkdir(parents=True)
    (stale_out / "export_moc3" / "old.moc3").write_bytes(_make_valid_moc3_bytes())
    monkeypatch.setattr(lp.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    seen_kwargs: list[dict] = []

    def capture_popen(*a, **kw):
        seen_kwargs.append(kw)
        return DummyProc()

    stale_report = lp.run_p2l_pipeline(
        out_dir=stale_out,
        name="stale",
        p2l_dir=p2l_dir,
        psd_path=psd_path,
        popen_fn=capture_popen,
        wait_fn=lambda *a, **kw: None,
        client_factory=lambda _dir, _ep, _tok: FakeP2LClient(fail_export="moc3", canvas_style="list"),
    )
    assert seen_kwargs[-1].get("creationflags") == 0x08000000
    assert stale_report["exports"]["moc3"]["ok"] is False
    assert stale_report["moc3_validation"]["ok"] is False

    # Empty renders, CREATE_NO_WINDOW=0, custom targets without moc3/vts
    out_min = tmp_path / "out_min"
    monkeypatch.setattr(lp.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    report3 = lp.run_p2l_pipeline(
        out_dir=out_min,
        name="min",
        p2l_dir=p2l_dir,
        psd_path=psd_path,
        post_rig_path=post_rig_path,
        targets=(("web", {}),),
        popen_fn=capture_popen,
        wait_fn=lambda *a, **kw: None,
        client_factory=lambda _dir, _ep, _tok: FakeP2LClient(
            fail_export="web", canvas_style="list", empty_renders=True
        ),
    )
    assert "creationflags" not in seen_kwargs[-1]
    assert report3["moc3_validation"] is None
    assert report3["vts_validation"] is None
    assert not (out_min / "poses_full.png").exists()

    # Empty renders with sabun_poses
    report4 = lp.run_p2l_pipeline(
        out_dir=tmp_path / "out_min2",
        name="min2",
        p2l_dir=p2l_dir,
        psd_path=psd_path,
        sabun_poses_path=sabun_one,
        popen_fn=lambda *a, **kw: DummyProc(),
        wait_fn=lambda *a, **kw: None,
        client_factory=lambda _dir, _ep, _tok: FakeP2LClient(canvas_style="list", empty_renders=True),
    )
    assert report4["exports"]["moc3"]["ok"] is True

    # Redact helper no-op on missing file
    lp.redact_token_in_file(tmp_path / "missing_log.txt", "tok")


def test_default_loaders(tmp_path: Path, monkeypatch):
    opened = []
    fake_psd_mod = SimpleNamespace(PSDImage=SimpleNamespace(open=lambda p: opened.append(p) or "psd_obj"))
    monkeypatch.setitem(sys.modules, "psd_tools", fake_psd_mod)
    assert lp._default_psd_loader(tmp_path / "a.psd") == "psd_obj"
    assert opened == [tmp_path / "a.psd"]

    # _default_client_factory probes macOS app/resources layout as well as root
    created = []
    fake_mcp_mod = SimpleNamespace(Client=lambda **kw: created.append(kw) or "client_obj")
    monkeypatch.setitem(sys.modules, "mcp_proxy", fake_mcp_mod)
    mac_dir = tmp_path / "mac_p2l"
    (mac_dir / "app" / "resources").mkdir(parents=True)
    (mac_dir / "app" / "resources" / "mcp_proxy.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert lp._default_client_factory(mac_dir, "http://127.0.0.1:1/mcp", "tok") == "client_obj"
    assert lp._default_client_factory(mac_dir, "http://127.0.0.1:1/mcp", "tok2") == "client_obj"
    assert str(mac_dir / "app" / "resources") in sys.path
    assert len(created) == 2


# ---------- CLI main() subcommands ----------


def test_cli_subcommands_and_error_handling(tmp_path: Path, monkeypatch, capsys):
    # validate-moc3 valid & invalid exit codes
    valid_dir = tmp_path / "m_ok"
    _write_valid_moc3_package(valid_dir)
    assert lp.main(["validate-moc3", str(valid_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # 5-byte fake MOC3\x05 with no model3.json exits 1
    fake_dir = tmp_path / "m_fake"
    fake_dir.mkdir()
    (fake_dir / "fake.moc3").write_bytes(b"MOC3\x05")
    assert lp.main(["validate-moc3", str(fake_dir)]) == 1
    capsys.readouterr()

    # patch-web
    web_cfg = tmp_path / "web_ui.json"
    web_cfg.write_text(json.dumps({"title": "CLI Test", "toggles": []}), encoding="utf-8")
    web_html = tmp_path / "out_web" / "index.html"
    assert lp.main(["patch-web", str(web_html), "--config", str(web_cfg)]) == 0
    assert web_html.exists()
    capsys.readouterr()

    # split-eyes (missing brow emits warning on stderr and in JSON)
    img = np.zeros((100, 200, 4), dtype=np.uint8)
    img[45:75, 25:65] = (10, 20, 30, 255)
    eye_png = tmp_path / "eyes_cli.png"
    Image.fromarray(img).save(eye_png)
    assert (
        lp.main(
            [
                "split-eyes",
                str(eye_png),
                "-o",
                str(tmp_path / "split_out"),
                "--prefix",
                "e",
                "--rect",
                "0,0,200,100",
            ]
        )
        == 0
    )
    cap = capsys.readouterr()
    assert json.loads(cap.out)["warnings"]
    assert "WARNING:" in cap.err

    # F11: split-eyes bad --rect (wrong count AND non-numeric) exits 2 cleanly
    assert (
        lp.main(
            ["split-eyes", str(eye_png), "-o", str(tmp_path / "split_out"), "--prefix", "e", "--rect", "1,2"]
        )
        == 2
    )
    assert (
        lp.main(
            ["split-eyes", str(eye_png), "-o", str(tmp_path / "split_out"), "--prefix", "e", "--rect", "a,b,c,d"]
        )
        == 2
    )
    capsys.readouterr()

    # recover-single
    monkeypatch.setattr(
        lp,
        "build_single_image_layout",
        lambda *a, **kw: {"layers": [], "name": kw.get("name", "m")},
    )
    assert (
        lp.main(
            [
                "recover-single",
                str(eye_png),
                str(tmp_path / "in.psd"),
                "-o",
                str(tmp_path / "rec_out"),
                "--name",
                "m",
                "--no-reproject",
            ]
        )
        == 0
    )
    capsys.readouterr()

    # F11: bad --p2l-dir returns exit 2 cleanly (no traceback)
    lay = tmp_path / "l.json"
    lay.write_text("{}", encoding="utf-8")
    assert (
        lp.main(
            ["run-p2l", "--layout", str(lay), "--out", str(tmp_path / "o"), "--name", "n", "--p2l-dir", str(tmp_path / "bad_p2l")]
        )
        == 2
    )
    assert "ERROR:" in capsys.readouterr().err

    # run-p2l happy path (zero dropped losses, valid moc3 & vts)
    monkeypatch.setattr(lp, "discover_p2l_dir", lambda explicit, env=None: tmp_path)
    monkeypatch.setattr(
        lp,
        "run_p2l_pipeline",
        lambda **kw: {
            "moc3_validation": {"ok": True, "moc3": {"header_ok": True}},
            "vts_validation": {"ok": True, "moc3": {"header_ok": True}},
            "export_losses": {"total_dropped": 0, "total_approximated": 0, "by_target": {}},
            "exports": {"moc3": {"ok": True}},
        },
    )
    assert lp.main(["run-p2l", "--layout", str(lay), "--out", str(tmp_path / "o"), "--name", "n"]) == 0
    capsys.readouterr()

    # F2: run-p2l fails (exit 1) on dropped export losses unless --allow-export-losses is set
    monkeypatch.setattr(
        lp,
        "run_p2l_pipeline",
        lambda **kw: {
            "moc3_validation": {"ok": True},
            "vts_validation": {"ok": True},
            "export_losses": {"total_dropped": 18, "total_approximated": 2, "by_target": {}},
            "exports": {"moc3": {"ok": True}},
        },
    )
    assert lp.main(["run-p2l", "--layout", str(lay), "--out", str(tmp_path / "o"), "--name", "n"]) == 1
    assert (
        lp.main(
            ["run-p2l", "--layout", str(lay), "--out", str(tmp_path / "o"), "--name", "n", "--allow-export-losses"]
        )
        == 0
    )
    capsys.readouterr()

    # F2: run-p2l fails when vts_validation.ok is False
    monkeypatch.setattr(
        lp,
        "run_p2l_pipeline",
        lambda **kw: {
            "moc3_validation": {"ok": True},
            "vts_validation": {"ok": False},
            "export_losses": {"total_dropped": 0, "total_approximated": 0, "by_target": {}},
            "exports": {"moc3": {"ok": True}, "vtube-studio": {"ok": True}},
        },
    )
    assert lp.main(["run-p2l", "--layout", str(lay), "--out", str(tmp_path / "o"), "--name", "n"]) == 1
    capsys.readouterr()

    # run-p2l fails on failed export target
    monkeypatch.setattr(
        lp,
        "run_p2l_pipeline",
        lambda **kw: {
            "moc3_validation": {"ok": True},
            "vts_validation": {"ok": True},
            "export_losses": {"total_dropped": 0, "total_approximated": 0, "by_target": {}},
            "exports": {"moc3": {"ok": True}, "gif": {"ok": False}},
        },
    )
    assert lp.main(["run-p2l", "--layout", str(lay), "--out", str(tmp_path / "o"), "--name", "n"]) == 1
    capsys.readouterr()
