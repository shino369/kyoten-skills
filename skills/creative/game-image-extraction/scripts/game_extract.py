#!/usr/bin/env python3
"""Unified CLI for inspecting, extracting, converting, and verifying images from Unity and KiriKiri2/Z games.

Verification is strictly metadata-only (file counts, zero-byte checks, PNG/JPEG header magic) and never decodes
or renders pixels, keeping sensitive/NSFW game assets safe from accidental viewing.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

XP3_MAGIC = b"XP3\r\n \n\x1a\x8b\x67\x01"
XP3_PROTECTED_FLAG = 0x80000000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPG_MAGIC = b"\xff\xd8\xff"
TLG_MAGICS = (b"TLG0", b"TLG5", b"TLG6")
PSB_MAGIC = b"PSB\x00"

INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DEFAULT_SKIP_BUNDLE_PREFIXES = (
    "general-voice",
    "naninovel-audio",
    "general-fonts",
    "naninovel-scripts",
    "general-localization",
    "naninovel-movies",
)


# ---------- Path helpers ----------


def sanitize_segment(name: str) -> str:
    cleaned = INVALID_WIN_CHARS.sub("_", name.strip()).rstrip(". ")
    return cleaned or "unnamed"


def sanitize_relpath(rel: str) -> Path:
    parts = [sanitize_segment(p) for p in rel.replace("\\", "/").split("/") if p and p not in (".", "..")]
    return Path(*parts) if parts else Path("unnamed")


def strip_prefix(path_str: str, prefixes: tuple[str, ...]) -> str:
    norm = path_str.replace("\\", "/")
    lower = norm.lower()
    for prefix in prefixes:
        if lower.startswith(prefix.lower()):
            return norm[len(prefix) :].lstrip("/")
    return norm.lstrip("/")


def unique_path(
    target_path: Path,
    path_id: int,
    used_paths: set[Path],
    *,
    suffix: str | None = ".png",
    replace_suffix: bool = False,
    lock: threading.Lock | None = None,
) -> Path:
    if suffix is None:
        base = target_path
    elif replace_suffix:
        base = target_path.with_suffix(suffix)
    else:
        base = target_path.with_name(target_path.name + suffix)

    def _reserve() -> Path:
        candidate = base
        if candidate in used_paths or candidate.exists():
            candidate = base.with_name(f"{base.stem}__{path_id}{base.suffix}")
            counter = 2
            while candidate in used_paths or candidate.exists():
                candidate = base.with_name(f"{base.stem}__{path_id}_{counter}{base.suffix}")
                counter += 1
        used_paths.add(candidate)
        return candidate

    if lock is not None:
        with lock:
            return _reserve()
    return _reserve()


# ---------- XP3 parsing & plain unpack ----------


def parse_xp3_index(xp3_path: Path) -> dict[str, Any]:
    with xp3_path.open("rb") as fp:
        hdr = fp.read(40)
        if hdr[:11] != XP3_MAGIC:
            raise ValueError(f"Not an XP3 archive: {xp3_path}")
        off_v1 = struct.unpack("<Q", hdr[11:19])[0]
        if off_v1 == 0x17:
            version = 2
            table_off = struct.unpack("<Q", hdr[32:40])[0]
        else:
            version = 1
            table_off = off_v1

        fp.seek(table_off)
        idx_flag = fp.read(1)[0]
        if idx_flag & 1:
            comp_sz, _orig_sz = struct.unpack("<QQ", fp.read(16))
            raw = zlib.decompress(fp.read(comp_sz))
        else:
            orig_sz = struct.unpack("<Q", fp.read(8))[0]
            raw = fp.read(orig_sz)

    has_hxv4 = False
    entries: list[dict[str, Any]] = []
    pos = 0
    while pos + 12 <= len(raw):
        tag = raw[pos : pos + 4]
        sz = struct.unpack("<Q", raw[pos + 4 : pos + 12])[0]
        body = raw[pos + 12 : pos + 12 + sz]
        pos += 12 + sz
        if tag == b"Hxv4":
            has_hxv4 = True
        elif tag == b"File":
            entry: dict[str, Any] = {"name": "", "flags": 0, "orig_size": 0, "adlr": 0, "segments": []}
            sub = 0
            while sub + 12 <= len(body):
                stag = body[sub : sub + 4]
                ssz = struct.unpack("<Q", body[sub + 4 : sub + 12])[0]
                sbody = body[sub + 12 : sub + 12 + ssz]
                sub += 12 + ssz
                if stag == b"adlr":
                    entry["adlr"] = struct.unpack("<I", sbody[:4])[0]
                elif stag == b"info":
                    flags, orig, _arch, nlen = struct.unpack("<IQQH", sbody[:22])
                    entry["flags"] = flags
                    entry["orig_size"] = orig
                    entry["name"] = sbody[22 : 22 + nlen * 2].decode("utf-16le", errors="replace")
                elif stag == b"segm":
                    for off in range(0, len(sbody), 28):
                        sflags, soff, sorig, sarch = struct.unpack("<IQQQ", sbody[off : off + 28])
                        entry["segments"].append(
                            {"flags": sflags, "offset": soff, "orig_size": sorig, "arch_size": sarch}
                        )
            entries.append(entry)

    return {"version": version, "has_hxv4": has_hxv4, "entries": entries}


def inspect_xp3(xp3_path: Path, *, max_entries: int = 10) -> dict[str, Any]:
    parsed = parse_xp3_index(xp3_path)
    entries = parsed["entries"]
    protected = sum(1 for e in entries if e["flags"] & XP3_PROTECTED_FLAG)
    samples = [
        {"name": e["name"], "orig_size": e["orig_size"], "protected": bool(e["flags"] & XP3_PROTECTED_FLAG)}
        for e in entries[:max_entries]
    ]
    return {
        "file": xp3_path.name,
        "version": parsed["version"],
        "has_hxv4": parsed["has_hxv4"],
        "entry_count": len(entries),
        "protected_count": protected,
        "sample_entries": samples,
    }


def unpack_xp3(xp3_path: Path, out_dir: Path) -> dict[str, Any]:
    parsed = parse_xp3_index(xp3_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    used: set[Path] = set()
    extracted = 0
    skipped_protected = 0

    with xp3_path.open("rb") as fp:
        for idx, entry in enumerate(parsed["entries"]):
            if entry["flags"] & XP3_PROTECTED_FLAG:
                skipped_protected += 1
                continue
            chunks = bytearray()
            for seg in entry["segments"]:
                fp.seek(seg["offset"])
                raw = fp.read(seg["arch_size"])
                chunks += zlib.decompress(raw) if (seg["flags"] & 1) else raw
            rel = sanitize_relpath(entry["name"])
            dst = unique_path(out_dir / rel, idx, used, suffix=None)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(bytes(chunks))
            extracted += 1

    return {
        "file": xp3_path.name,
        "has_hxv4": parsed["has_hxv4"],
        "extracted": extracted,
        "skipped_protected": skipped_protected,
    }


# ---------- Engine detection ----------


def detect_game(game_dir: Path) -> dict[str, Any]:
    if not game_dir.is_dir():
        raise FileNotFoundError(f"Game directory not found: {game_dir}")

    data_dirs = sorted(p for p in game_dir.iterdir() if p.is_dir() and p.name.endswith("_Data"))
    if data_dirs:
        data_dir = data_dirs[0]
        aa_dir = data_dir / "StreamingAssets" / "aa"
        bundles = sorted(aa_dir.rglob("*.bundle")) if aa_dir.is_dir() else []
        root_assets = sorted(p.name for p in data_dir.iterdir() if p.is_file() and p.suffix == ".assets")
        return {
            "engine": "unity",
            "game_dir": str(game_dir),
            "unity": {
                "data_dir": str(data_dir),
                "bundle_count": len(bundles),
                "bundle_mb": round(sum(p.stat().st_size for p in bundles) / (1024 * 1024), 2),
                "has_naninovel": any("naninovel" in str(p).lower() for p in bundles),
                "root_assets": root_assets,
            },
        }

    xp3_files = sorted(p for p in game_dir.iterdir() if p.is_file() and p.suffix.lower() == ".xp3")
    if xp3_files:
        archives: list[dict[str, Any]] = []
        has_hxv4 = False
        has_protected = False
        for xp3 in xp3_files:
            try:
                meta = inspect_xp3(xp3, max_entries=0)
                has_hxv4 = has_hxv4 or meta["has_hxv4"]
                has_protected = has_protected or meta["protected_count"] > 0
                archives.append(
                    {
                        "file": xp3.name,
                        "mb": round(xp3.stat().st_size / (1024 * 1024), 2),
                        "version": meta["version"],
                        "has_hxv4": meta["has_hxv4"],
                        "entry_count": meta["entry_count"],
                        "protected_count": meta["protected_count"],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                archives.append({"file": xp3.name, "error": str(exc)})
        sig_count = sum(1 for p in game_dir.iterdir() if p.is_file() and p.suffix.lower() == ".sig")
        return {
            "engine": "kirikiri",
            "game_dir": str(game_dir),
            "kirikiri": {
                "archive_count": len(xp3_files),
                "has_hxv4": has_hxv4,
                "has_protected": has_protected,
                "sig_count": sig_count,
                "archives": archives,
            },
        }

    return {"engine": "unknown", "game_dir": str(game_dir)}


# ---------- Raw file magic classification & conversion ----------


def classify_magic(path: Path) -> str:
    with path.open("rb") as fp:
        head = fp.read(8)
    if head.startswith(PNG_MAGIC):
        return "png"
    if head.startswith(JPG_MAGIC):
        return "jpg"
    if head[:4] in TLG_MAGICS:
        return "tlg"
    if head[:4] == PSB_MAGIC:
        return "psb"
    return "other"


KNOWN_RAW_EXTS = {".bin", ".png", ".jpg", ".jpeg", ".tlg", ".psb", ".pimg"}


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def _safe_run(runner: Callable[[list[str]], Any], cmd: list[str]) -> Any:
    try:
        return runner(cmd)
    except OSError as exc:
        return SimpleNamespace(returncode=-1, stderr=str(exc))


def convert_raw_dir(
    raw_dir: Path,
    out_dir: Path,
    *,
    tlg2png: Path | None = None,
    psb_parser: Path | None = None,
    runner: Callable[[list[str]], Any] | None = None,
    workers: int = 6,
) -> dict[str, Any]:
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"Raw directory not found: {raw_dir}")

    t0 = time.perf_counter()
    run_cmd = runner or _default_runner
    out_dir.mkdir(parents=True, exist_ok=True)

    tasks: list[tuple[int, str, Path, Path, str]] = []
    idx = 0
    for arch_dir in sorted(raw_dir.iterdir()):
        if not arch_dir.is_dir():
            continue
        for src_file in sorted(arch_dir.rglob("*")):
            if not src_file.is_file():
                continue
            kind = classify_magic(src_file)
            if kind != "other":
                tasks.append((idx, arch_dir.name, src_file, src_file.relative_to(arch_dir), kind))
                idx += 1

    used: set[Path] = set()
    lock = threading.Lock()

    def handle(item: tuple[int, str, Path, Path, str]) -> dict[str, Any]:
        item_id, archive, src, rel, kind = item
        replace_ext = rel.suffix.lower() in KNOWN_RAW_EXTS
        if kind in ("png", "jpg"):
            dst = unique_path(
                out_dir / archive / rel,
                item_id,
                used,
                suffix=f".{kind}",
                replace_suffix=replace_ext,
                lock=lock,
            )
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return {"archive": archive, "kind": kind, "status": "ok", "outputs": 1}

        if kind == "tlg":
            if tlg2png is None:
                return {"archive": archive, "kind": "tlg", "status": "error", "outputs": 0, "error": f"{src.name}: --tlg2png not provided"}
            dst = unique_path(
                out_dir / archive / rel,
                item_id,
                used,
                suffix=".png",
                replace_suffix=replace_ext,
                lock=lock,
            )
            dst.parent.mkdir(parents=True, exist_ok=True)
            proc = _safe_run(run_cmd, [str(tlg2png), str(src), str(dst)])
            if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
                return {"archive": archive, "kind": "tlg", "status": "ok", "outputs": 1}
            dst.unlink(missing_ok=True)
            return {
                "archive": archive,
                "kind": "tlg",
                "status": "error",
                "outputs": 0,
                "error": f"{src.name}: rc={proc.returncode} {proc.stderr.strip()[:200]}",
            }

        # kind == "psb"
        if psb_parser is None:
            return {"archive": archive, "kind": "psb", "status": "error", "outputs": 0, "error": f"{src.name}: --psb-parser not provided"}
        rel_dir = rel.with_suffix("") if replace_ext else rel
        dst_dir = unique_path(out_dir / f"{archive}_pimg" / rel_dir, item_id, used, suffix=None, lock=lock)
        proc = _safe_run(run_cmd, [sys.executable, str(psb_parser), "compose-all", str(src), "-o", str(dst_dir)])
        if proc.returncode == 0 and dst_dir.exists():
            pngs = list(dst_dir.rglob("*.png"))
            if pngs:
                return {"archive": archive, "kind": "psb_composed", "status": "ok", "outputs": len(pngs)}
        shutil.rmtree(dst_dir, ignore_errors=True)

        fb_dir = unique_path(out_dir / f"{archive}_psb_raw" / rel_dir, item_id, used, suffix=None, lock=lock)
        proc2 = _safe_run(run_cmd, [sys.executable, str(psb_parser), "extract-all", str(src), "-o", str(fb_dir), "--png"])
        if proc2.returncode == 0 and fb_dir.exists():
            imgs = list(fb_dir.rglob("*.png"))
            if imgs:
                return {"archive": archive, "kind": "psb_extracted", "status": "ok", "outputs": len(imgs)}
        shutil.rmtree(fb_dir, ignore_errors=True)

        if proc.returncode == 0 and proc2.returncode == 0:
            return {"archive": archive, "kind": "psb_non_image", "status": "skipped", "outputs": 0}
        err_detail = " | ".join(s for s in (proc.stderr.strip(), proc2.stderr.strip()) if s)[:200]
        return {
            "archive": archive,
            "kind": "psb",
            "status": "error",
            "outputs": 0,
            "error": f"{src.name}: compose_rc={proc.returncode} extract_rc={proc2.returncode} {err_detail}",
        }

    summary: dict[str, Any] = {}
    errors: list[str] = []
    non_image_psb = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(handle, tasks):
            arch_stat = summary.setdefault(res["archive"], {})
            k_stat = arch_stat.setdefault(res["kind"], {"files": 0, "outputs": 0, "errors": 0})
            k_stat["files"] += 1
            k_stat["outputs"] += res["outputs"]
            if res["status"] == "error":
                k_stat["errors"] += 1
                errors.append(res["error"])
            elif res["status"] == "skipped":
                non_image_psb += 1

    report = {
        "elapsed_seconds": round(time.perf_counter() - t0, 2),
        "summary": summary,
        "error_count": len(errors),
        "errors": errors[:100],
        "non_image_psb_count": non_image_psb,
    }
    (out_dir / "convert_summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


# ---------- Unity extraction ----------


def default_unity_loader() -> Callable[[str], Any]:
    try:
        import UnityPy  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "UnityPy is required for Unity extraction. Run with: "
            "uv run --python 3.12 --with UnityPy --with pillow python game_extract.py unity-extract ..."
        ) from exc
    return UnityPy.load


def _build_container_map(env: Any) -> dict[int, str]:
    return {obj.path_id: cpath for cpath, obj in env.container.items() if hasattr(obj, "path_id")}


def _save_pil(
    img: Any,
    target: Path,
    path_id: int,
    used: set[Path],
    lock: threading.Lock,
    *,
    replace_suffix: bool = False,
) -> None:
    dst = unique_path(target, path_id, used, suffix=".png", replace_suffix=replace_suffix, lock=lock)
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, format="PNG")


def _try_save_obj(
    obj: Any,
    source_name: str,
    target_fn: Callable[[Any, Any], tuple[Path, bool] | None],
    used: set[Path],
    lock: threading.Lock,
    errors: list[str],
) -> int:
    try:
        data = obj.read()
        img = data.image
        if img.width == 0 or img.height == 0:
            return 0
        resolved = target_fn(data, img)
        if resolved is None:
            return 0
        target, replace_suffix = resolved
        _save_pil(img, target, obj.path_id, used, lock, replace_suffix=replace_suffix)
        return 1
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{source_name}:{obj.path_id}: {exc}")
        return 0


def _extract_unity_source(
    source_path: Path,
    category: str,
    out_root: Path,
    load_env: Callable[[str], Any],
    used: set[Path],
    lock: threading.Lock,
    *,
    skip_sprites: bool,
) -> dict[str, Any]:
    try:
        env = load_env(str(source_path))
    except Exception as exc:  # noqa: BLE001
        return {"category": category, "saved": 0, "errors": [f"{source_path.name}: {exc}"]}

    cmap = _build_container_map(env)
    saved = 0
    errors: list[str] = []

    if category == "characters":
        char_dir = out_root / "Characters" / sanitize_segment(source_path.stem)
        for obj in env.objects:
            kind = obj.type.name
            if kind == "Texture2D" or (kind == "Sprite" and not skip_sprites):
                sub = "sheets" if kind == "Texture2D" else "parts"
                saved += _try_save_obj(
                    obj,
                    source_path.name,
                    lambda d, _i, s=sub, k=kind: (char_dir / s / sanitize_segment(d.m_Name or f"{k}_{obj.path_id}"), False),
                    used,
                    lock,
                    errors,
                )
        return {"category": category, "saved": saved, "errors": errors}

    if category == "backgrounds":
        prefixes = ("Assets/Textures/Naninovel/Backgrounds/", "Assets/Textures/", "Assets/")
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            cpath = cmap.get(obj.path_id)
            saved += _try_save_obj(
                obj,
                source_path.name,
                lambda d, _i, cp=cpath: (
                    (out_root / "Backgrounds" / sanitize_relpath(strip_prefix(cp, prefixes)), True)
                    if cp
                    else (out_root / "Backgrounds" / sanitize_segment(d.m_Name or f"tex_{obj.path_id}"), False)
                ),
                used,
                lock,
                errors,
            )
        return {"category": category, "saved": saved, "errors": errors}

    if category == "ui_sprites":
        prefixes = ("Assets/Textures/Packed/", "Assets/Textures/", "Assets/")
        for obj in env.objects:
            kind = obj.type.name
            if kind == "Sprite" and not skip_sprites:
                cpath = cmap.get(obj.path_id)
                saved += _try_save_obj(
                    obj,
                    source_path.name,
                    lambda d, _i, cp=cpath: (
                        (out_root / "UI_Sprites" / sanitize_relpath(strip_prefix(cp, prefixes)), True)
                        if cp
                        else (out_root / "UI_Sprites" / "Unsorted" / sanitize_segment(d.m_Name or f"s_{obj.path_id}"), False)
                    ),
                    used,
                    lock,
                    errors,
                )
            elif kind == "Texture2D":
                saved += _try_save_obj(
                    obj,
                    source_path.name,
                    lambda d, _i: None
                    if (d.m_Name or "").startswith("sactx-")
                    else (out_root / "UI_Sprites" / "Standalone" / sanitize_segment(d.m_Name or f"tex_{obj.path_id}"), False),
                    used,
                    lock,
                    errors,
                )
        return {"category": category, "saved": saved, "errors": errors}

    # spawn or other/root
    is_spawn = category == "spawn"
    base_dir = out_root / "Spawn_Events_CutIns" if is_spawn else out_root / "Other" / category
    tex_subdir = base_dir / "textures" if is_spawn else base_dir
    spr_subdir = base_dir / "cropped_sprites" if is_spawn else base_dir / "sprites"
    tex_sizes: dict[str, set[tuple[int, int]]] = {}

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue

        def _tex_target(d: Any, img: Any) -> tuple[Path, bool]:
            name = d.m_Name or f"tex_{obj.path_id}"
            tex_sizes.setdefault(name, set()).add(img.size)
            return tex_subdir / sanitize_segment(name), False

        saved += _try_save_obj(obj, source_path.name, _tex_target, used, lock, errors)

    if not skip_sprites:
        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue

            def _spr_target(d: Any, img: Any) -> tuple[Path, bool] | None:
                name = d.m_Name or f"spr_{obj.path_id}"
                if name in tex_sizes and img.size in tex_sizes[name]:
                    return None
                return spr_subdir / sanitize_segment(name), False

            saved += _try_save_obj(obj, source_path.name, _spr_target, used, lock, errors)

    return {"category": category, "saved": saved, "errors": errors}


def _classify_unity_bundle(rel_low: str, bundle_path: Path) -> str:
    name = bundle_path.name.lower()
    if "naninovel-backgrounds" in rel_low:
        return "backgrounds"
    if "naninovel-characters" in rel_low:
        return "characters"
    if "general-sprites" in name:
        return "ui_sprites"
    if "naninovel-spawn" in name:
        return "spawn"
    return sanitize_segment(bundle_path.stem)


def extract_unity_images(
    data_dir: Path,
    out_dir: Path,
    *,
    load_env: Callable[[str], Any] | None = None,
    include_all: bool = False,
    skip_sprites: bool = False,
    workers: int = 6,
) -> dict[str, Any]:
    if not data_dir.is_dir():
        raise FileNotFoundError(f"Unity data directory not found: {data_dir}")

    t0 = time.perf_counter()
    loader = load_env or default_unity_loader()
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[Path, str]] = []
    aa_dir = data_dir / "StreamingAssets" / "aa"
    if aa_dir.is_dir():
        for bundle in sorted(aa_dir.rglob("*.bundle")):
            rel_low = str(bundle.relative_to(aa_dir)).lower().replace("\\", "/")
            if not include_all and any(p in rel_low for p in DEFAULT_SKIP_BUNDLE_PREFIXES):
                continue
            jobs.append((bundle, _classify_unity_bundle(rel_low, bundle)))

    for root_file in sorted(data_dir.iterdir()):
        if root_file.is_file() and root_file.suffix == ".assets":
            jobs.append((root_file, f"RootAssets/{sanitize_segment(root_file.stem)}"))

    used: set[Path] = set()
    lock = threading.Lock()
    summary: dict[str, int] = {}
    errors: list[str] = []

    def run_job(job: tuple[Path, str]) -> dict[str, Any]:
        return _extract_unity_source(job[0], job[1], out_dir, loader, used, lock, skip_sprites=skip_sprites)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(run_job, jobs):
            summary[res["category"]] = summary.get(res["category"], 0) + res["saved"]
            errors.extend(res["errors"])

    manifest = {
        "source": str(data_dir),
        "output": str(out_dir),
        "elapsed_seconds": round(time.perf_counter() - t0, 2),
        "summary": summary,
        "error_count": len(errors),
        "errors": errors[:200],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


# ---------- Metadata-only verification ----------


def verify_images(images_dir: Path) -> dict[str, Any]:
    if not images_dir.is_dir():
        raise FileNotFoundError(f"Image directory not found: {images_dir}")

    total = 0
    zero_byte = 0
    invalid = 0
    total_bytes = 0

    for path in sorted(images_dir.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg"):
            continue
        total += 1
        size = path.stat().st_size
        total_bytes += size
        if size == 0:
            zero_byte += 1
            invalid += 1
            continue
        with path.open("rb") as fp:
            head = fp.read(8)
        if ext == ".png" and not head.startswith(PNG_MAGIC):
            invalid += 1
        elif ext in (".jpg", ".jpeg") and not head.startswith(JPG_MAGIC):
            invalid += 1

    return {
        "directory": str(images_dir),
        "total": total,
        "zero_byte": zero_byte,
        "invalid": invalid,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
    }


# ---------- CLI ----------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Inspect, extract, convert, and verify Unity and KiriKiri2/Z game images.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_det = sub.add_parser("detect", help="Detect game engine and archive layout (read-only)")
    p_det.add_argument("game_dir", type=Path)
    p_det.add_argument("--json", action="store_true")

    p_uni = sub.add_parser("unity-extract", help="Extract Texture2D and Sprite images from Unity *_Data")
    p_uni.add_argument("data_dir", type=Path)
    p_uni.add_argument("-o", "--out", type=Path, required=True)
    p_uni.add_argument("--include-all", action="store_true")
    p_uni.add_argument("--skip-sprites", action="store_true")
    p_uni.add_argument("--workers", type=int, default=6)

    p_xi = sub.add_parser("xp3-inspect", help="Inspect XP3 header, version, Hxv4 and protection flags")
    p_xi.add_argument("xp3_path", type=Path)
    p_xi.add_argument("--max-entries", type=int, default=10)
    p_xi.add_argument("--json", action="store_true")

    p_xu = sub.add_parser("xp3-unpack", help="Unpack unencrypted XP3 archives (skips protected entries)")
    p_xu.add_argument("xp3_path", type=Path)
    p_xu.add_argument("-o", "--out", type=Path, required=True)

    p_cv = sub.add_parser("convert-raw", help="Convert extracted raw files (PNG/JPG/TLG/PSB-PIMG)")
    p_cv.add_argument("raw_dir", type=Path)
    p_cv.add_argument("-o", "--out", type=Path, required=True)
    p_cv.add_argument("--tlg2png", type=Path, default=None)
    p_cv.add_argument("--psb-parser", type=Path, default=None)
    p_cv.add_argument("--workers", type=int, default=6)

    p_vf = sub.add_parser("verify", help="Metadata-only verification (never decodes or views pixels)")
    p_vf.add_argument("images_dir", type=Path)
    p_vf.add_argument("--json", action="store_true")
    return ap


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for k, v in payload.items():
            print(f"{k}: {v}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "detect":
        _emit(detect_game(args.game_dir), args.json)
        return 0
    if args.cmd == "unity-extract":
        res = extract_unity_images(
            args.data_dir,
            args.out,
            include_all=args.include_all,
            skip_sprites=args.skip_sprites,
            workers=args.workers,
        )
        _emit(res, True)
        return 0 if res["error_count"] == 0 else 1
    if args.cmd == "xp3-inspect":
        _emit(inspect_xp3(args.xp3_path, max_entries=args.max_entries), args.json)
        return 0
    if args.cmd == "xp3-unpack":
        _emit(unpack_xp3(args.xp3_path, args.out), True)
        return 0
    if args.cmd == "convert-raw":
        res = convert_raw_dir(
            args.raw_dir,
            args.out,
            tlg2png=args.tlg2png,
            psb_parser=args.psb_parser,
            workers=args.workers,
        )
        _emit(res, True)
        return 0 if res["error_count"] == 0 else 1
    # args.cmd == "verify"
    try:
        res = verify_images(args.images_dir)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _emit(res, args.json)
    return 0 if res["invalid"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
