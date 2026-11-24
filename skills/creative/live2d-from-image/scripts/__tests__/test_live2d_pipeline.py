"""Tests for live2d_pipeline.py (100% branch coverage)."""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import live2d_pipeline as lp  # noqa: E402


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
    # Deep inside occluded zone stays See-through RGB
    assert out[2, 20, 2] > 180
    # Deep inside visible zone adopts crisp original RGB and boosted alpha
    assert out[35, 20, 0] > 180
    assert out[35, 20, 3] == 255


def test_split_eyes_and_brows_extracts_eyes_brows_and_skips_noise(tmp_path: Path):
    img = np.zeros((100, 200, 4), dtype=np.uint8)
    # Viewer-left (character right) brow & eye
    img[10:18, 20:60] = (40, 30, 30, 255)
    img[45:80, 25:75] = (80, 90, 200, 255)
    # Viewer-right (character left) brow & eye
    img[10:18, 140:180] = (40, 30, 30, 255)
    img[45:80, 125:175] = (80, 90, 200, 255)
    # Tiny 2-pixel noise speck (should be skipped)
    img[95, 95:97] = (255, 255, 255, 255)

    src = tmp_path / "eyes.png"
    Image.fromarray(img).save(src)
    out_parts = tmp_path / "parts"

    res = lp.split_eyes_and_brows(
        src,
        out_parts,
        "char_eye",
        {"left": 100.0, "top": 200.0, "width": 400.0, "height": 200.0},
    )
    assert set(res.keys()) == {"eye_r", "eye_l", "brow_r", "brow_l"}
    for key, (p, sz, rect) in res.items():
        assert p.exists()
        assert sz[0] > 0 and sz[1] > 0
        assert rect["width"] > 0 and rect["height"] > 0

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


def test_build_single_image_layout_recovers_hat_and_shoes_and_reprojects(tmp_path: Path):
    res = 200
    orig = np.zeros((res, res, 4), dtype=np.uint8)
    # Hat in top 22% (rows 4..30, cols 70..130)
    orig[4:30, 70:130] = (30, 20, 60, 255)
    # Main body/face in center (rows 40..150, cols 60..140)
    orig[40:150, 60:140] = (240, 210, 190, 255)
    # Shoes in bottom 22% (rows 165..195, cols 65..135)
    orig[165:195, 65:135] = (50, 30, 20, 255)
    # Tiny speck in top zone (below min_top_px, should be ignored)
    orig[2:4, 2:4] = (255, 0, 0, 255)

    src_rgba = tmp_path / "char.png"
    Image.fromarray(orig).save(src_rgba)

    leg_img = Image.new("RGBA", (80, 50), (100, 100, 120, 255))
    face_img = Image.new("RGBA", (80, 60), (200, 180, 170, 255))
    hw_img = Image.new("RGBA", (40, 10), (180, 40, 40, 255))
    extra_img = Image.new("RGBA", (20, 20), (10, 200, 10, 255))
    fake_psd = FakePSD(
        width=res,
        layers=[
            FakePSDLayer("empty_layer", (0, 0, 10, 10), None),
            FakePSDLayer("legwear", (60, 100, 140, 150), leg_img),
            FakePSDLayer("face", (60, 40, 140, 100), face_img),
            FakePSDLayer("custom_unmapped", (80, 60, 100, 80), extra_img),
            FakePSDLayer("headwear", (80, 30, 120, 40), hw_img),
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
    assert manifest["name"] == "demo"
    ids = [l["id"] for l in manifest["layers"]]
    assert "shoes_recovered" in ids
    assert "hat_recovered" in ids
    # Hat inserted before headwear
    assert ids.index("hat_recovered") < ids.index("st_04_headwear")
    assert (out_dir / "layout.json").exists()

    # No-reproject, overhanging layer bbox, out-of-bounds layer, and unsafe layer names (r1-6, r1-9)
    orig2 = np.zeros((res, res, 4), dtype=np.uint8)
    orig2[40:100, 60:140] = (240, 210, 190, 255)
    src2 = tmp_path / "char2.png"
    Image.fromarray(orig2).save(src2)
    overhang_img = Image.new("RGBA", (20, 20), (10, 20, 30, 255))
    fake_psd2 = FakePSD(
        width=res,
        layers=[
            FakePSDLayer("/../../escaped", (60, 40, 140, 100), face_img),
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
    # Verify all saved part paths live strictly inside out2/parts
    for lay in m2["layers"]:
        part_file = (out2 / lay["image"]["path"]).resolve()
        assert part_file.parent == (out2 / "parts").resolve()
        assert part_file.exists()
    # Overhanging layer clipped cleanly to [0, res)
    assert m2["layers"][1]["rect"] == {"left": 0.0, "top": 10.0, "width": 15.0, "height": 20.0}
    assert m2["layers"][2]["rect"] == {"left": 190.0, "top": 10.0, "width": 10.0, "height": 20.0}

    # Landscape letterboxed image (200x100 -> pad_y=50, nh=100) recovers hat/shoes inside [pad_y, pad_y+nh) (r2-2)
    land = np.zeros((100, 200, 4), dtype=np.uint8)
    land[2:18, 70:130] = (30, 20, 60, 255)  # hat in top 22% of image rows
    land[25:75, 60:140] = (240, 210, 190, 255)  # covered body
    land[82:98, 70:130] = (50, 30, 20, 255)  # shoes in bottom 22% of image rows
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
    land_ids = [l["id"] for l in m_land["layers"]]
    assert "hat_recovered" in land_ids
    assert "shoes_recovered" in land_ids


# ---------- Web viewer patching & MOC3 validation ----------


def test_patch_web_viewer_escapes_html_and_script_tags(tmp_path: Path):
    index = tmp_path / "web" / "index.html"
    cfg = {
        "title": "Test <Model> & Co",
        "prop_expression_ids": ["exp_hat_off"],
        "toggles": [
            {
                "id": "hat_off",
                "label": "</script><img onerror=1>",
                "parameter": "ParamHat",
                "on_value": 0.0,
                "off_value": 1.0,
            }
        ],
    }
    lp.patch_web_viewer(index, cfg)
    text = index.read_text(encoding="utf-8")
    assert "<title>Test &lt;Model&gt; &amp; Co</title>" in text
    assert "</script><img" not in text
    assert "<\\/script><img" in text
    assert "P2LPlayer.create" in text
    assert "p2lReady" in text
    assert "enforceRequires" in text
    assert "while (reqId" in text


def test_validate_moc3_dir_handles_missing_empty_and_valid(tmp_path: Path):
    missing = lp.validate_moc3_dir(tmp_path / "nope")
    assert missing["error"] == "directory missing"

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    empty_res = lp.validate_moc3_dir(empty_dir)
    assert empty_res["moc3"] is None
    assert empty_res["model3"] is None

    valid_dir = tmp_path / "valid"
    valid_dir.mkdir()
    (valid_dir / "hero.moc3").write_bytes(b"MOC3\x05\x00" + b"\x00" * 32)
    (valid_dir / "hero.model3.json").write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": "hero.moc3",
                    "Textures": ["hero.4096/texture_00.png"],
                    "Physics": "hero.physics3.json",
                    "DisplayInfo": "hero.cdi3.json",
                    "Expressions": [{"Name": "Smile", "File": "exp.json"}],
                    "Motions": {"Idle": [{"File": "idle.motion3.json"}]},
                },
                "Groups": [{"Name": "EyeBlink", "Ids": ["ParamEyeLOpen"]}],
            }
        ),
        encoding="utf-8",
    )
    res = lp.validate_moc3_dir(valid_dir)
    assert res["moc3"]["header_ok"] is True
    assert res["moc3"]["version"] == 5
    assert res["moc3"]["big_endian"] is False
    assert res["model3"]["expressions"] == ["Smile"]
    assert res["model3"]["motions"] == {"Idle": 1}
    assert res["model3"]["groups"] == ["EyeBlink"]

    # Truncated moc3 header
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "tiny.moc3").write_bytes(b"MO")
    bad_res = lp.validate_moc3_dir(bad_dir)
    assert bad_res["moc3"]["header_ok"] is False
    assert bad_res["moc3"]["version"] is None


# ---------- PSD2Live discovery, command builder & wait_for_mcp ----------


def test_discover_p2l_dir_explicit_env_and_errors(tmp_path: Path):
    p2l = tmp_path / "psd2live"
    (p2l / "app").mkdir(parents=True)

    assert lp.discover_p2l_dir(explicit=p2l) == p2l.resolve()
    assert lp.discover_p2l_dir(explicit=None, env={"PSD2LIVE_DIR": str(p2l)}) == p2l.resolve()

    with pytest.raises(FileNotFoundError, match="PSD2LIVE_DIR"):
        lp.discover_p2l_dir(explicit=None, env={})

    no_app = tmp_path / "incomplete"
    no_app.mkdir()
    with pytest.raises(FileNotFoundError, match="app"):
        lp.discover_p2l_dir(explicit=no_app)


def test_build_p2l_mcp_cmd_java_variants(tmp_path: Path):
    p2l = tmp_path / "p2l"
    (p2l / "app").mkdir(parents=True)
    store = tmp_path / "store"

    # Fallback to PATH "java"
    cmd_path = lp.build_p2l_mcp_cmd(p2l, 23881, "tok", store)
    assert cmd_path[0] == "java"
    assert "io.github.psd2live.MainKt" in cmd_path

    # POSIX bundled java
    posix_java = p2l / "runtime" / "bin" / "java"
    posix_java.parent.mkdir(parents=True)
    posix_java.write_text("", encoding="utf-8")
    cmd_posix = lp.build_p2l_mcp_cmd(p2l, 23881, "tok", store)
    assert cmd_posix[0] == str(posix_java)

    # Windows bundled java.exe takes precedence if present
    win_java = p2l / "runtime" / "bin" / "java.exe"
    win_java.write_text("", encoding="utf-8")
    cmd_win = lp.build_p2l_mcp_cmd(p2l, 23881, "tok", store)
    assert cmd_win[0] == str(win_java)


def test_wait_for_mcp_success_http_error_early_exit_and_timeout():
    class AliveProc:
        def poll(self):
            return None

    class DeadProc:
        returncode = 7

        def poll(self):
            return 7

    # 200 OK immediately
    lp.wait_for_mcp(23881, AliveProc(), timeout=5.0, opener=lambda _u, timeout=1.0: object())

    # 401 HTTPError means server is up
    def raise_401(url, timeout=1.0):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    lp.wait_for_mcp(23881, AliveProc(), timeout=5.0, opener=raise_401)

    # Early process exit
    with pytest.raises(RuntimeError, match="exited early"):
        lp.wait_for_mcp(23881, DeadProc(), timeout=5.0)

    # Retry once then timeout
    ticks = iter([0.0, 0.5, 2.0])

    def fail_conn(_u, timeout=1.0):
        raise ConnectionRefusedError("refused")

    with pytest.raises(TimeoutError, match="did not start"):
        lp.wait_for_mcp(
            23881,
            AliveProc(),
            timeout=1.0,
            opener=fail_conn,
            sleep_fn=lambda _s: None,
            time_fn=lambda: next(ticks),
        )


# ---------- apply_post_rig & run_p2l_pipeline ----------


class FakeP2LClient:
    def __init__(
        self,
        *,
        fail_export: str | None = None,
        fail_axis: bool = False,
        canvas_style: str = "list",
        empty_renders: bool = False,
    ):
        self.edits: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail_export = fail_export
        self.fail_axis = fail_axis
        self.canvas_style = canvas_style
        self.empty_renders = empty_renders

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
                "parameters": {
                    "items": [
                        {"id": "ParamAngleX"},
                        {"id": "ParamHat"},
                    ]
                },
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
                    # Simulate paginated 64 + 1 items
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
                (out_dir / "demo.moc3").write_bytes(b"MOC3\x05\x00" + b"\x00" * 10)
            if kwargs["target"] == "web":
                (out_dir / "index.html").write_text("<html>old</html>", encoding="utf-8")
            return {"exported": kwargs["target"]}
        return {"ok": True}


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
    assert summary["warnings"]  # missing layers/deformers reported, not silently dropped
    # Unresolved layer:/deformer: targets must be skipped rather than sent raw to PSD2Live (r2-3)
    axis_edits = [kw for t, kw in client.edits if t == "author_axis"]
    for ae in axis_edits:
        for sh in ae["shapes"]:
            assert not sh["target"].startswith(("layer:", "deformer:"))
    kf_edits = [kw for t, kw in client.edits if t == "keyform_apply"]
    assert len(kf_edits) == 1 and len(kf_edits[0]["changes"]) == 1

    # Verify strict ordering: motion_scale -> expression_put -> clamp ParamAngleX -> canvas_warp -> Phase B author_axis
    tool_seq = [t for t, _ in client.edits]
    assert tool_seq.index("motion_put") < tool_seq.index("expression_put")
    assert tool_seq.index("expression_put") < tool_seq.index("canvas_warp")
    clamp_idx = next(
        i for i, (t, kw) in enumerate(client.edits) if t == "parameter_update" and "min" in kw
    )
    assert clamp_idx < tool_seq.index("canvas_warp")

    # author_axis with ONLY unresolved targets skips the MCP edit cleanly
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

    # Also test error propagation in author_axis
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

    # Empty recipe + no motion changes branch
    empty_client = FakeP2LClient()
    empty_summary = lp.apply_post_rig(
        empty_client,
        {"motion_scale": {"clips": ["Idle"], "parameters": {"ParamNonExistent": 0.5}}},
    )
    assert empty_summary["rebound"] == 0


def test_run_p2l_pipeline_layout_and_psd_modes(tmp_path: Path, monkeypatch):
    p2l_dir = tmp_path / "p2l"
    (p2l_dir / "app").mkdir(parents=True)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text("{}", encoding="utf-8")
    expr_path = tmp_path / "exprs.json"
    expr_path.write_text(json.dumps([{"id": "exp_1", "name": "E1", "entries": []}]), encoding="utf-8")
    post_rig_path = tmp_path / "post_rig.json"
    post_rig_path.write_text(
        json.dumps({"web_ui": {"title": "Hero", "toggles": []}}), encoding="utf-8"
    )
    extra_poses = tmp_path / "extra.json"
    extra_poses.write_text(json.dumps([{"ParamAngleX": 10.0}]), encoding="utf-8")
    sabun_poses = tmp_path / "sabun.json"
    sabun_poses.write_text(
        json.dumps(
            {
                "viewport": {"left": 0, "top": 0, "width": 500, "height": 500},
                "columns": 3,
                "poses": [{}] * 9,  # triggers multi-page chunking (>8)
            }
        ),
        encoding="utf-8",
    )

    terminated = []

    class DummyProc:
        def __init__(self, *, timeout_on_wait: bool = False):
            self._timeout = timeout_on_wait
            self.killed = False

        def poll(self):
            return None

        def terminate(self):
            terminated.append("term")

        def wait(self, timeout=5):
            if self._timeout:
                raise lp.subprocess.TimeoutExpired(cmd="java", timeout=timeout)

        def kill(self):
            self.killed = True

    out_dir = tmp_path / "out_layout"
    (out_dir / "_mcp_store").mkdir(parents=True)  # exercise cleanup of stale _mcp_store

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
        popen_fn=lambda *a, **kw: DummyProc(),
        wait_fn=lambda *a, **kw: None,
        client_factory=lambda _dir, _ep, _tok: FakeP2LClient(fail_export="gif", canvas_style="dict"),
    )
    assert report["exports"]["moc3"]["ok"] is True
    assert report["exports"]["gif"]["ok"] is False
    assert (out_dir / "poses_full.png").exists()
    assert (out_dir / "poses_face.png").exists()
    assert (out_dir / "poses_sabun_1.png").exists()
    assert (out_dir / "poses_sabun_2.png").exists()
    assert "P2LPlayer.create" in (out_dir / "export_web" / "index.html").read_text(encoding="utf-8")

    # PSD mode + no post_rig + kill fallback on wait timeout + single-page sabun + non-list/dict canvas
    psd_path = tmp_path / "char.psd"
    psd_path.write_bytes(b"8BPS")
    sabun_one = tmp_path / "sabun1.json"
    sabun_one.write_text(
        json.dumps({"viewport": {"left": 0, "top": 0, "width": 100, "height": 100}, "poses": [{}]}),
        encoding="utf-8",
    )
    proc2 = DummyProc(timeout_on_wait=True)
    out_psd = tmp_path / "out_psd"
    report2 = lp.run_p2l_pipeline(
        out_dir=out_psd,
        name="psd_hero",
        p2l_dir=p2l_dir,
        psd_path=psd_path,
        expressions_path=expr_path,
        sabun_poses_path=sabun_one,
        popen_fn=lambda *a, **kw: proc2,
        wait_fn=lambda *a, **kw: None,
        client_factory=lambda _dir, _ep, _tok: FakeP2LClient(canvas_style="other"),
    )
    assert proc2.killed is True
    assert (out_psd / "poses_sabun.png").exists()
    assert report2["exports"]["moc3"]["ok"] is True

    # Must provide exactly one of layout_path or psd_path
    with pytest.raises(ValueError, match="exactly one"):
        lp.run_p2l_pipeline(out_dir=tmp_path / "x", name="x", p2l_dir=p2l_dir)

    # Nonexistent optional config paths must fail fast with FileNotFoundError (r1-7)
    with pytest.raises(FileNotFoundError, match="post_rig_path"):
        lp.run_p2l_pipeline(
            out_dir=tmp_path / "missing_cfg",
            name="x",
            p2l_dir=p2l_dir,
            layout_path=layout_path,
            post_rig_path=tmp_path / "does_not_exist.json",
        )

    # Malformed face_rect must fail fast BEFORE launching PSD2Live MCP (r2-5)
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

    # Re-running with failed moc3 export clears stale export_moc3 so old .moc3 cannot pass (r1-5)
    stale_out = tmp_path / "stale_out"
    (stale_out / "export_moc3").mkdir(parents=True)
    (stale_out / "export_moc3" / "old.moc3").write_bytes(b"MOC3\x05\x00" + b"\x00" * 10)
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
    assert stale_report["moc3_validation"]["moc3"] is None

    # List canvas, empty renders, no expressions/sabun, web export failing with web_ui, CREATE_NO_WINDOW=0 (r1-1)
    out_min = tmp_path / "out_min"
    monkeypatch.setattr(lp.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    report3 = lp.run_p2l_pipeline(
        out_dir=out_min,
        name="min",
        p2l_dir=p2l_dir,
        psd_path=psd_path,
        post_rig_path=post_rig_path,
        popen_fn=capture_popen,
        wait_fn=lambda *a, **kw: None,
        client_factory=lambda _dir, _ep, _tok: FakeP2LClient(
            fail_export="web", canvas_style="list", empty_renders=True
        ),
    )
    assert "creationflags" not in seen_kwargs[-1]
    assert report3["exports"]["web"]["ok"] is False
    assert not (out_min / "poses_full.png").exists()

    # Also test empty_renders with sabun_poses + no post_rig/expressions
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


def test_default_loaders(tmp_path: Path, monkeypatch):
    # _default_psd_loader
    opened = []
    fake_psd_mod = SimpleNamespace(PSDImage=SimpleNamespace(open=lambda p: opened.append(p) or "psd_obj"))
    monkeypatch.setitem(sys.modules, "psd_tools", fake_psd_mod)
    assert lp._default_psd_loader(tmp_path / "a.psd") == "psd_obj"
    assert opened == [tmp_path / "a.psd"]

    # _default_client_factory (both when p2l_dir not in sys.path and already in sys.path)
    created = []
    fake_mcp_mod = SimpleNamespace(Client=lambda **kw: created.append(kw) or "client_obj")
    monkeypatch.setitem(sys.modules, "mcp_proxy", fake_mcp_mod)
    fake_dir = tmp_path / "mcp_dir"
    fake_dir.mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert lp._default_client_factory(fake_dir, "http://127.0.0.1:1/mcp", "tok") == "client_obj"
    assert lp._default_client_factory(fake_dir, "http://127.0.0.1:1/mcp", "tok2") == "client_obj"
    assert len(created) == 2


# ---------- CLI main() subcommands ----------


def test_cli_subcommands(tmp_path: Path, monkeypatch, capsys):
    # validate-moc3 valid & invalid exit codes
    valid_dir = tmp_path / "m_ok"
    valid_dir.mkdir()
    (valid_dir / "a.moc3").write_bytes(b"MOC3\x05\x00" + b"\x00" * 8)
    assert lp.main(["validate-moc3", str(valid_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["moc3"]["header_ok"] is True

    assert lp.main(["validate-moc3", str(tmp_path / "missing_dir")]) == 1
    capsys.readouterr()

    # patch-web
    web_cfg = tmp_path / "web_ui.json"
    web_cfg.write_text(json.dumps({"title": "CLI Test", "toggles": []}), encoding="utf-8")
    web_html = tmp_path / "out_web" / "index.html"
    assert lp.main(["patch-web", str(web_html), "--config", str(web_cfg)]) == 0
    assert web_html.exists()

    # split-eyes
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
    capsys.readouterr()

    # split-eyes invalid rect
    assert (
        lp.main(
            ["split-eyes", str(eye_png), "-o", str(tmp_path / "split_out"), "--prefix", "e", "--rect", "1,2"]
        )
        == 2
    )

    # recover-single via monkeypatched build_single_image_layout
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

    # run-p2l via monkeypatched discover_p2l_dir & run_p2l_pipeline
    monkeypatch.setattr(lp, "discover_p2l_dir", lambda explicit, env=None: tmp_path)
    monkeypatch.setattr(
        lp,
        "run_p2l_pipeline",
        lambda **kw: {"moc3_validation": {"moc3": {"header_ok": True}}, "exports": {"moc3": {"ok": True}}},
    )
    lay = tmp_path / "l.json"
    lay.write_text("{}", encoding="utf-8")
    assert lp.main(["run-p2l", "--layout", str(lay), "--out", str(tmp_path / "o"), "--name", "n"]) == 0

    # run-p2l with any failed export target returns 1 even if moc3 header_ok is True (r1-5)
    monkeypatch.setattr(
        lp,
        "run_p2l_pipeline",
        lambda **kw: {
            "moc3_validation": {"moc3": {"header_ok": True}},
            "exports": {"moc3": {"ok": True}, "gif": {"ok": False}},
        },
    )
    assert lp.main(["run-p2l", "--layout", str(lay), "--out", str(tmp_path / "o"), "--name", "n"]) == 1

    # run-p2l missing moc3 returns 1
    monkeypatch.setattr(
        lp,
        "run_p2l_pipeline",
        lambda **kw: {"moc3_validation": {"moc3": None}, "exports": {"moc3": {"ok": True}}},
    )
    assert lp.main(["run-p2l", "--layout", str(lay), "--out", str(tmp_path / "o"), "--name", "n"]) == 1
