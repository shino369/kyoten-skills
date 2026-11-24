"""Tests for game_extract.py (run through uv; 100% branch coverage)."""

from __future__ import annotations

import json
import pathlib
import runpy
import struct
import sys
import threading
import zlib
from types import SimpleNamespace

import pytest
from PIL import Image

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import game_extract as ge  # noqa: E402

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
TLG_BYTES = b"TLG0.0\x00sds\x1a\x00" + b"\x00" * 8
PSB_BYTES = b"PSB\x00" + b"\x00" * 16


# ---------- XP3 builder helper ----------


def build_chunk(tag: bytes, body: bytes) -> bytes:
    return tag + struct.pack("<Q", len(body)) + body


def build_xp3(
    entries: list[dict],
    *,
    version: int = 2,
    compress_index: bool = True,
    hxv4_blob: bytes | None = None,
) -> bytes:
    """Build a synthetic KiriKiri XP3 v1 or v2 archive for tests."""
    magic = b"XP3\r\n \n\x1a\x8b\x67\x01"
    header_len = 19 if version == 1 else 40
    data_chunks = bytearray()
    index_chunks = bytearray()

    if hxv4_blob is not None:
        index_chunks += build_chunk(b"Hxv4", hxv4_blob)
    index_chunks += build_chunk(b"Meta", b"\x00\x01")

    for entry in entries:
        segs_raw = bytearray()
        total_orig = 0
        total_arch = 0
        for seg_data, seg_comp in entry["segments"]:
            off = header_len + len(data_chunks)
            raw_seg = zlib.compress(seg_data) if seg_comp else seg_data
            data_chunks += raw_seg
            sflags = 1 if seg_comp else 0
            segs_raw += struct.pack("<IQQQ", sflags, off, len(seg_data), len(raw_seg))
            total_orig += len(seg_data)
            total_arch += len(raw_seg)

        name_u16 = entry["name"].encode("utf-16le")
        info_body = (
            struct.pack("<IQQH", entry.get("flags", 0), total_orig, total_arch, len(entry["name"]))
            + name_u16
        )
        file_body = (
            build_chunk(b"adlr", struct.pack("<I", entry.get("adlr", 0x12345678)))
            + build_chunk(b"segm", bytes(segs_raw))
            + build_chunk(b"info", info_body)
            + build_chunk(b"time", b"\x00" * 8)
        )
        index_chunks += build_chunk(b"File", file_body)

    table_off = header_len + len(data_chunks)
    if compress_index:
        comp = zlib.compress(bytes(index_chunks))
        index_block = b"\x01" + struct.pack("<QQ", len(comp), len(index_chunks)) + comp
    else:
        index_block = b"\x00" + struct.pack("<Q", len(index_chunks)) + bytes(index_chunks)

    if version == 1:
        header = magic + struct.pack("<Q", table_off)
    else:
        header = magic + struct.pack("<QIBQQ", 0x17, 1, 0x80, 0, table_off)

    return header + bytes(data_chunks) + index_block


# ---------- Path helpers ----------


def test_path_helpers(tmp_path):
    assert ge.sanitize_segment(' a:b*c?"<>| ') == "a_b_c_____"
    assert ge.sanitize_segment("...") == "unnamed"
    assert ge.sanitize_relpath("../a/b\\c.png") == pathlib.Path("a", "b", "c.png")
    assert ge.sanitize_relpath("../..") == pathlib.Path("unnamed")
    assert ge.strip_prefix("Assets/Textures/Bg/a.png", ("Assets/Textures/",)) == "Bg/a.png"
    assert ge.strip_prefix("Other/a.png", ("Assets/",)) == "Other/a.png"

    # replace_suffix=True replaces known extension; replace_suffix=False preserves dotted m_Name
    used: set[pathlib.Path] = set()
    p1 = ge.unique_path(tmp_path / "img.tga", 10, used, replace_suffix=True)
    assert p1 == tmp_path / "img.png"
    p2 = ge.unique_path(tmp_path / "img.tga", 10, used, replace_suffix=True)
    assert p2 == tmp_path / "img__10.png"
    p3 = ge.unique_path(tmp_path / "img.tga", 10, used, replace_suffix=True)
    assert p3 == tmp_path / "img__10_2.png"

    # Dotted m_Name must not truncate trailing segment (r1-8)
    lock = threading.Lock()
    d1 = ge.unique_path(tmp_path / "UI.Button.Normal", 1, used, suffix=".png", replace_suffix=False, lock=lock)
    d2 = ge.unique_path(tmp_path / "UI.Button.Hover", 2, used, suffix=".png", replace_suffix=False, lock=lock)
    assert d1 == tmp_path / "UI.Button.Normal.png"
    assert d2 == tmp_path / "UI.Button.Hover.png"

    # Disk collision without in-memory used set
    p1.write_bytes(PNG_BYTES)
    p4 = ge.unique_path(tmp_path / "img.tga", 30, set(), replace_suffix=True)
    assert p4 == tmp_path / "img__30.png"


# ---------- Engine detection ----------


def test_detect_unity_and_kirikiri_and_unknown(tmp_path):
    u_dir = tmp_path / "unity_game"
    aa = u_dir / "demo_Data" / "StreamingAssets" / "aa" / "StandaloneWindows64"
    (aa / "naninovel-backgrounds_assets_naninovel").mkdir(parents=True)
    (aa / "naninovel-backgrounds_assets_naninovel" / "bg1.bundle").write_bytes(b"x" * 20)
    (aa / "general-voice-act01.bundle").write_bytes(b"v" * 30)
    (u_dir / "demo_Data" / "resources.assets").write_bytes(b"r" * 10)

    info_u = ge.detect_game(u_dir)
    assert info_u["engine"] == "unity"
    assert info_u["unity"]["bundle_count"] == 2
    assert info_u["unity"]["has_naninovel"] is True

    u2 = tmp_path / "unity_plain"
    (u2 / "plain_Data").mkdir(parents=True)
    (u2 / "plain_Data" / "sharedassets0.assets").write_bytes(b"a")
    info_u2 = ge.detect_game(u2)
    assert info_u2["engine"] == "unity"
    assert info_u2["unity"]["bundle_count"] == 0

    k_dir = tmp_path / "krkr_game"
    (k_dir / "plugin").mkdir(parents=True)
    (k_dir / "plugin" / "yuzuex.dll").write_bytes(b"MZ")
    (k_dir / "data.xp3").write_bytes(
        build_xp3([{"name": "bg/a.png", "segments": [(PNG_BYTES, False)]}], version=1, compress_index=False)
    )
    (k_dir / "image.xp3").write_bytes(
        build_xp3(
            [{"name": "\u5000", "flags": 0x80000000, "segments": [(b"enc", False)]}],
            version=2,
            hxv4_blob=b"\x01\x02",
        )
    )
    (k_dir / "image.xp3.sig").write_bytes(b"sig")
    (k_dir / "broken.xp3").write_bytes(b"NOT_XP3")

    info_k = ge.detect_game(k_dir)
    assert info_k["engine"] == "kirikiri"
    assert info_k["kirikiri"]["has_hxv4"] is True
    assert info_k["kirikiri"]["has_protected"] is True
    assert info_k["kirikiri"]["sig_count"] == 1
    assert any("error" in a for a in info_k["kirikiri"]["archives"])

    empty = tmp_path / "empty"
    empty.mkdir()
    assert ge.detect_game(empty)["engine"] == "unknown"

    with pytest.raises(FileNotFoundError):
        ge.detect_game(tmp_path / "does_not_exist")

    not_dir = tmp_path / "plain_file"
    not_dir.write_bytes(b"x")
    with pytest.raises(FileNotFoundError):
        ge.detect_game(not_dir)


# ---------- XP3 inspect & unpack ----------


def test_xp3_inspect_and_unpack(tmp_path):
    bad = tmp_path / "bad.xp3"
    bad.write_bytes(b"JUNK")
    with pytest.raises(ValueError, match="Not an XP3"):
        ge.parse_xp3_index(bad)

    xp3 = tmp_path / "sample.xp3"
    xp3.write_bytes(
        build_xp3(
            [
                {"name": "bg/test1.png", "segments": [(PNG_BYTES[:10], True), (PNG_BYTES[10:], False)]},
                {"name": "../../escape.jpg", "segments": [(JPG_BYTES, False)]},
                {"name": "secret.png", "flags": 0x80000000, "segments": [(b"xxxx", False)]},
            ],
            version=2,
            compress_index=True,
            hxv4_blob=b"\xaa\xbb",
        )
    )

    meta = ge.inspect_xp3(xp3, max_entries=2)
    assert meta["version"] == 2
    assert meta["has_hxv4"] is True
    assert meta["entry_count"] == 3
    assert meta["protected_count"] == 1
    assert len(meta["sample_entries"]) == 2

    out = tmp_path / "unpacked"
    res = ge.unpack_xp3(xp3, out)
    assert res["extracted"] == 2
    assert res["skipped_protected"] == 1
    assert (out / "bg" / "test1.png").read_bytes() == PNG_BYTES
    assert (out / "escape.jpg").read_bytes() == JPG_BYTES

    # Colliding entries including pre-existing a__2.png never overwrite (r1-4)
    dup_xp3 = tmp_path / "dup.xp3"
    dup_xp3.write_bytes(
        build_xp3(
            [
                {"name": "a.png", "segments": [(b"111", False)]},
                {"name": "a__2.png", "segments": [(b"222", False)]},
                {"name": "a.png", "segments": [(b"333", False)]},
            ],
            version=1,
            compress_index=False,
        )
    )
    dup_out = tmp_path / "dup_out"
    res_dup = ge.unpack_xp3(dup_xp3, dup_out)
    assert res_dup["extracted"] == 3
    assert (dup_out / "a.png").read_bytes() == b"111"
    assert (dup_out / "a__2.png").read_bytes() == b"222"
    assert (dup_out / "a__2_2.png").read_bytes() == b"333"


# ---------- Magic classify & convert_raw ----------


def test_classify_magic_and_convert_raw(tmp_path):
    # Missing raw_dir must not create out_dir (r2-7)
    missing_out = tmp_path / "should_not_exist"
    with pytest.raises(FileNotFoundError):
        ge.convert_raw_dir(tmp_path / "missing_raw", missing_out)
    assert not missing_out.exists()

    raw = tmp_path / "raw"
    # Nested subdirectories, non-.bin extensions (r1-1), colliding stems (r2-1, r2-2)
    (raw / "bg" / "nested").mkdir(parents=True)
    (raw / "bg" / "nested" / "park.png").write_bytes(PNG_BYTES)
    (raw / "bg" / "nested" / "park.tlg").write_bytes(TLG_BYTES)
    (raw / "bg" / "bg.01").write_bytes(PNG_BYTES)
    (raw / "bg" / "bg.02").write_bytes(PNG_BYTES)
    (raw / "bg" / "a.png").write_bytes(PNG_BYTES)
    (raw / "bg" / "b.jpg").write_bytes(JPG_BYTES)
    (raw / "bg" / "d.tlg").write_bytes(TLG_BYTES)
    # Same-stem PSB collision: e.pimg (composed CG) + e.psb (non-image PSB) (r2-1)
    (raw / "bg" / "e.pimg").write_bytes(PSB_BYTES)
    (raw / "bg" / "e.psb").write_bytes(PSB_BYTES)
    (raw / "bg" / "f.psb").write_bytes(PSB_BYTES)
    (raw / "bg" / "h.pimg").write_bytes(PSB_BYTES)
    (raw / "bg" / "note.txt").write_bytes(b"hello text")
    (raw / "stray.txt").write_bytes(b"ignored")

    assert ge.classify_magic(raw / "bg" / "a.png") == "png"
    assert ge.classify_magic(raw / "bg" / "b.jpg") == "jpg"
    assert ge.classify_magic(raw / "bg" / "nested" / "park.tlg") == "tlg"
    assert ge.classify_magic(raw / "bg" / "e.pimg") == "psb"
    assert ge.classify_magic(raw / "bg" / "note.txt") == "other"

    def fake_runner(cmd):
        cmd_str = " ".join(cmd)
        if "tlg2png" in cmd[0]:
            src, dst = pathlib.Path(cmd[1]), pathlib.Path(cmd[2])
            if src.name == "park.tlg":
                dst.write_bytes(PNG_BYTES)
                return SimpleNamespace(returncode=0, stderr="")
            # Simulate partial/0-byte file left on failure (r2-5)
            dst.write_bytes(b"")
            return SimpleNamespace(returncode=1, stderr="bad tlg")
        if "compose-all" in cmd_str:
            src = pathlib.Path(cmd[3])
            out_dir = pathlib.Path(cmd[5])
            out_dir.mkdir(parents=True, exist_ok=True)
            if src.name == "e.pimg":
                (out_dir / "cg_01.png").write_bytes(PNG_BYTES)
                return SimpleNamespace(returncode=0, stderr="")
            if src.name == "e.psb":
                return SimpleNamespace(returncode=0, stderr="")
            return SimpleNamespace(returncode=1, stderr="compose boom")
        # extract-all fallback
        src = pathlib.Path(cmd[3])
        out_dir = pathlib.Path(cmd[5])
        out_dir.mkdir(parents=True, exist_ok=True)
        if src.name == "f.psb":
            (out_dir / "embedded.png").write_bytes(PNG_BYTES)
            return SimpleNamespace(returncode=0, stderr="")
        if src.name == "e.psb":
            return SimpleNamespace(returncode=0, stderr="")
        # h.pimg fails compose-all (stderr="compose boom") and extract-all rc=0 w/ 0 imgs (r2-8)
        return SimpleNamespace(returncode=0, stderr="")

    out = tmp_path / "images"
    report = ge.convert_raw_dir(
        raw,
        out,
        tlg2png=pathlib.Path("tlg2png.exe"),
        psb_parser=pathlib.Path("psb_parser.py"),
        runner=fake_runner,
        workers=2,
    )
    assert report["error_count"] == 2  # d.tlg + h.pimg
    assert any("compose boom" in e for e in report["errors"])  # r2-8
    assert report["non_image_psb_count"] == 1  # e.psb only
    # Colliding park.png + park.tlg both preserved (r2-2)
    assert len(list((out / "bg" / "nested").glob("*.png"))) == 2
    # Dotted extensionless bg.01 + bg.02 both preserved (r2-2)
    assert (out / "bg" / "bg.01.png").exists()
    assert (out / "bg" / "bg.02.png").exists()
    # Partial d.png cleaned up on tlg2png failure (r2-5)
    assert not (out / "bg" / "d.png").exists()
    # e.pimg's composed PNG survived sibling e.psb (r2-1)
    assert len(list((out / "bg_pimg").rglob("cg_01.png"))) == 1
    assert (out / "bg_psb_raw" / "f" / "embedded.png").exists()
    assert not (out / "bg_pimg" / "h").exists()
    assert not (out / "bg_psb_raw" / "h").exists()
    assert (out / "convert_summary.json").exists()

    # Missing external tools report clean errors
    report_no_tools = ge.convert_raw_dir(raw, tmp_path / "images_no_tools", workers=1)
    assert report_no_tools["error_count"] == 6  # 2 tlg + 4 psb

    # OSError / FileNotFoundError from runner is caught cleanly (r1-6)
    def raising_runner(_cmd):
        raise FileNotFoundError("no such exe")

    report_oserr = ge.convert_raw_dir(
        raw,
        tmp_path / "images_oserr",
        tlg2png=pathlib.Path("missing_tlg2png"),
        psb_parser=pathlib.Path("missing_psb.py"),
        runner=raising_runner,
        workers=1,
    )
    assert report_oserr["error_count"] == 6
    assert (tmp_path / "images_oserr" / "convert_summary.json").exists()

    # Default runner uses UTF-8 with errors='replace' (r2-4)
    proc = ge._default_runner([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xffok')"])
    assert proc.returncode == 0
    assert "ok" in proc.stdout


# ---------- Unity extraction ----------


class FakeObj:
    def __init__(self, path_id, type_name, name, size=(4, 4), fail=False):
        self.path_id = path_id
        self.type = SimpleNamespace(name=type_name)
        self._name = name
        self._size = size
        self._fail = fail

    def read(self):
        if self._fail:
            raise RuntimeError("decode failed")
        w, h = self._size
        img = Image.new("RGBA", (max(w, 1), max(h, 1)), (255, 0, 0, 255))
        if w == 0 or h == 0:
            img = SimpleNamespace(width=0, height=0, size=(0, 0))
        return SimpleNamespace(m_Name=self._name, m_Width=w, m_Height=h, image=img)


def test_unity_extract_all_categories_and_concurrency(tmp_path):
    # Missing data_dir must not create out_dir (r2-7)
    missing_uout = tmp_path / "missing_uout"
    with pytest.raises(FileNotFoundError):
        ge.extract_unity_images(tmp_path / "missing_Data", missing_uout, load_env=lambda p: p)
    assert not missing_uout.exists()

    # Ancestor dir named naninovel-backgrounds must not misclassify non-bg bundles (r2-3)
    data_dir = tmp_path / "naninovel-backgrounds-ancestor" / "game_Data"
    aa = data_dir / "StreamingAssets" / "aa" / "StandaloneWindows64"
    (aa / "naninovel-backgrounds_assets_naninovel").mkdir(parents=True)
    (aa / "naninovel-characters_assets_naninovel").mkdir(parents=True)

    bg_b1 = aa / "naninovel-backgrounds_assets_naninovel" / "1_1.bundle"
    bg_b2 = aa / "naninovel-backgrounds_assets_naninovel" / "1_2.bundle"
    bg_b3 = aa / "naninovel-backgrounds_assets_naninovel" / "1_3.bundle"
    ch_b = aa / "naninovel-characters_assets_naninovel" / "ema.bundle"
    ui_b = aa / "general-sprites_assets_all.bundle"
    sp_b = aa / "naninovel-spawn_assets_all.bundle"
    vo_b = aa / "general-voice-act01.bundle"
    ot_b = aa / "general-transitions_assets_all.bundle"
    corrupt_b = aa / "corrupt_assets_all.bundle"
    rt_a = data_dir / "resources.assets"
    for p in (bg_b1, bg_b2, bg_b3, ch_b, ui_b, sp_b, vo_b, ot_b, corrupt_b, rt_a):
        p.write_bytes(b"stub")

    # 3 background bundles with colliding name and identical path_id=1 (r1-3, r1-4) + containerless bg (r2-6)
    o_bg1 = FakeObj(1, "Texture2D", "BG")
    o_bg2 = FakeObj(1, "Texture2D", "BG")
    o_bg3 = FakeObj(1, "Texture2D", "BG")
    o_bg_nocont = FakeObj(5, "Texture2D", "LooseBG")
    o_bg_zero = FakeObj(2, "Texture2D", "Empty", size=(0, 0))
    o_bg_err = FakeObj(3, "Texture2D", "Broken", fail=True)
    o_ch_tex = FakeObj(10, "Texture2D", "Ema_Sheet", size=(8, 8))
    o_ch_spr = FakeObj(11, "Sprite", "Ema.Eye.01", size=(2, 2))
    o_ch_spr2 = FakeObj(12, "Sprite", "Ema.Eye.02", size=(2, 2))
    o_ch_zero = FakeObj(13, "Sprite", "Ema_Zero", size=(0, 0))
    o_ch_other = FakeObj(14, "MonoBehaviour", "Script")
    o_ui_spr = FakeObj(30, "Sprite", "Btn_Start")
    o_ui_unsorted = FakeObj(33, "Sprite", "LooseIcon")
    o_ui_atlas = FakeObj(31, "Texture2D", "sactx-0-1024-UI")
    o_ui_standalone = FakeObj(32, "Texture2D", "InputIndicator")
    o_sp_tex = FakeObj(40, "Texture2D", "CutIn_1", size=(6, 6))
    o_sp_dup = FakeObj(41, "Sprite", "CutIn_1", size=(6, 6))
    o_sp_crop = FakeObj(42, "Sprite", "CutIn_Crop", size=(3, 3))
    o_ot = FakeObj(50, "Texture2D", "Fade")
    o_rt_tex = FakeObj(60, "Texture2D", "LogoAtlas")
    o_rt_spr = FakeObj(61, "Sprite", "BrandLogo", size=(2, 2))
    o_vo = FakeObj(70, "Texture2D", "VoiceThumb")

    envs = {
        str(bg_b1): SimpleNamespace(
            objects=[o_bg1, o_bg_nocont, o_bg_zero, o_bg_err, FakeObj(4, "AssetBundle", "AB")],
            container={"Assets/Textures/Main/BG.png": o_bg1},
        ),
        str(bg_b2): SimpleNamespace(objects=[o_bg2], container={"Assets/Textures/Main/BG.png": o_bg2}),
        str(bg_b3): SimpleNamespace(objects=[o_bg3], container={"Assets/Textures/Main/BG.png": o_bg3}),
        str(ch_b): SimpleNamespace(objects=[o_ch_tex, o_ch_spr, o_ch_spr2, o_ch_zero, o_ch_other], container={}),
        str(ui_b): SimpleNamespace(
            objects=[o_ui_spr, o_ui_unsorted, o_ui_atlas, o_ui_standalone],
            container={"Assets/Textures/UI/Btn.png": o_ui_spr},
        ),
        str(sp_b): SimpleNamespace(objects=[o_sp_tex, o_sp_dup, o_sp_crop], container={}),
        str(ot_b): SimpleNamespace(objects=[o_ot], container={}),
        str(rt_a): SimpleNamespace(objects=[o_rt_tex, o_rt_spr], container={}),
        str(vo_b): SimpleNamespace(objects=[o_vo], container={}),
    }

    def load_env(path: str):
        if path == str(corrupt_b):
            raise ValueError("corrupt bundle")
        return envs[path]

    out = tmp_path / "unity_out"
    manifest = ge.extract_unity_images(data_dir, out, load_env=load_env, workers=4)
    # 1 decode error + 1 corrupt bundle load error (r1-7)
    assert manifest["error_count"] == 2
    assert len(list((out / "Backgrounds" / "Main").glob("*.png"))) == 3
    assert (out / "Backgrounds" / "LooseBG.png").exists()
    assert (out / "Characters" / "ema" / "sheets" / "Ema_Sheet.png").exists()
    assert (out / "Characters" / "ema" / "parts" / "Ema.Eye.01.png").exists()
    assert (out / "Characters" / "ema" / "parts" / "Ema.Eye.02.png").exists()
    assert (out / "UI_Sprites" / "UI" / "Btn.png").exists()
    assert (out / "UI_Sprites" / "Unsorted" / "LooseIcon.png").exists()
    assert (out / "UI_Sprites" / "Standalone" / "InputIndicator.png").exists()
    assert not any("sactx-" in p.name for p in (out / "UI_Sprites").rglob("*.png"))
    assert (out / "Spawn_Events_CutIns" / "textures" / "CutIn_1.png").exists()
    assert (out / "Spawn_Events_CutIns" / "cropped_sprites" / "CutIn_Crop.png").exists()
    assert not (out / "Spawn_Events_CutIns" / "cropped_sprites" / "CutIn_1.png").exists()
    assert (out / "Other" / "general-transitions_assets_all" / "Fade.png").exists()
    assert (out / "Other" / "RootAssets" / "resources" / "LogoAtlas.png").exists()
    assert (out / "Other" / "RootAssets" / "resources" / "sprites" / "BrandLogo.png").exists()
    assert not (out / "Other" / "general-voice-act01").exists()

    # include_all=True includes voice bundle; skip_sprites=True skips sprites
    out2 = tmp_path / "unity_out_all"
    ge.extract_unity_images(data_dir, out2, load_env=load_env, include_all=True, skip_sprites=True, workers=1)
    assert (out2 / "Other" / "general-voice-act01" / "VoiceThumb.png").exists()
    assert not (out2 / "Characters" / "ema" / "parts").exists()

    # Unity dir with no StreamingAssets/aa (root assets only)
    plain_data = tmp_path / "plain_Data"
    plain_data.mkdir()
    plain_a = plain_data / "sharedassets0.assets"
    plain_a.write_bytes(b"stub")
    (plain_data / "not_assets.txt").write_bytes(b"x")
    plain_env = {str(plain_a): SimpleNamespace(objects=[FakeObj(99, "Shader", "S")], container={})}
    ge.extract_unity_images(plain_data, tmp_path / "plain_out", load_env=plain_env.__getitem__, workers=1)


def test_unity_loader_present_and_missing(monkeypatch):
    fake_mod = SimpleNamespace(load=lambda p: p)
    monkeypatch.setitem(sys.modules, "UnityPy", fake_mod)
    assert ge.default_unity_loader() is fake_mod.load

    monkeypatch.setitem(sys.modules, "UnityPy", None)
    with pytest.raises(RuntimeError, match="UnityPy is required"):
        ge.default_unity_loader()


# ---------- Metadata-only verification ----------


def test_verify_images_metadata_only(tmp_path):
    with pytest.raises(FileNotFoundError):
        ge.verify_images(tmp_path / "nonexistent")

    not_a_dir = tmp_path / "file.png"
    not_a_dir.write_bytes(PNG_BYTES)
    with pytest.raises(FileNotFoundError):
        ge.verify_images(not_a_dir)

    d = tmp_path / "imgs"
    (d / "sub").mkdir(parents=True)
    (d / "ok.png").write_bytes(PNG_BYTES)
    (d / "sub" / "ok.jpg").write_bytes(JPG_BYTES)
    (d / "readme.txt").write_bytes(b"not an image")

    ok_res = ge.verify_images(d)
    assert ok_res["total"] == 2
    assert ok_res["zero_byte"] == 0
    assert ok_res["invalid"] == 0

    (d / "empty.png").write_bytes(b"")
    (d / "bad.png").write_bytes(b"NOTPNG!!")
    (d / "bad.jpg").write_bytes(b"NOTJPG!!")

    bad_res = ge.verify_images(d)
    assert bad_res["total"] == 5
    assert bad_res["zero_byte"] == 1
    assert bad_res["invalid"] == 3


# ---------- CLI ----------


def test_cli_subcommands_and_entrypoint(tmp_path, monkeypatch, capsys):
    k_dir = tmp_path / "g"
    k_dir.mkdir()
    xp3 = k_dir / "plain.xp3"
    xp3.write_bytes(build_xp3([{"name": "pic.png", "segments": [(PNG_BYTES, False)]}], version=1))

    assert ge.main(["detect", str(k_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["engine"] == "kirikiri"

    assert ge.main(["detect", str(k_dir)]) == 0
    assert "kirikiri" in capsys.readouterr().out

    assert ge.main(["xp3-inspect", str(xp3), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["entry_count"] == 1

    unp = tmp_path / "unp"
    assert ge.main(["xp3-unpack", str(xp3), "-o", str(unp)]) == 0
    capsys.readouterr()
    assert (unp / "pic.png").read_bytes() == PNG_BYTES

    raw = tmp_path / "raw"
    (raw / "bg").mkdir(parents=True)
    (raw / "bg" / "x.png").write_bytes(PNG_BYTES)
    img_out = tmp_path / "out_imgs"
    assert ge.main(["convert-raw", str(raw), "-o", str(img_out)]) == 0
    capsys.readouterr()
    assert (img_out / "bg" / "x.png").exists()

    # verify (pass, corrupt, missing)
    assert ge.main(["verify", str(img_out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["invalid"] == 0

    assert ge.main(["verify", str(tmp_path / "missing_dir")]) == 1
    assert "not found" in capsys.readouterr().err.lower()

    (img_out / "bg" / "corrupt.png").write_bytes(b"bad")
    assert ge.main(["verify", str(img_out)]) == 1
    capsys.readouterr()

    monkeypatch.setattr(ge, "extract_unity_images", lambda *a, **kw: {"error_count": 0, "summary": {}})
    assert ge.main(["unity-extract", str(k_dir), "-o", str(tmp_path / "u_out")]) == 0
    capsys.readouterr()

    # CLI non-zero exit codes for unity-extract and convert-raw (r2-6)
    monkeypatch.setattr(ge, "extract_unity_images", lambda *a, **kw: {"error_count": 1, "summary": {}})
    assert ge.main(["unity-extract", str(k_dir), "-o", str(tmp_path / "u_out")]) == 1
    capsys.readouterr()

    (raw / "bg" / "need_tlg.tlg").write_bytes(TLG_BYTES)
    assert ge.main(["convert-raw", str(raw), "-o", str(tmp_path / "out_imgs_err")]) == 1
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["game_extract.py", "detect", str(k_dir)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPTS / "game_extract.py"), run_name="__main__")
    assert exc.value.code == 0
