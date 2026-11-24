# KiriKiri2 / KiriKiriZ (`.xp3`) Image Extraction Reference

Detailed workflow for inspecting, unpacking, and converting images from KiriKiri2 and KiriKiriZ visual novels.

## Table of Contents

- [Identifying XP3 Protection Type](#identifying-xp3-protection-type)
- [Path A: Unencrypted / Standard XP3](#path-a-unencrypted--standard-xp3)
- [Path B: Encrypted `Hxv4` / `CxdecV2` XP3 (Offline Static Recovery)](#path-b-encrypted-hxv4--cxdecv2-xp3-offline-static-recovery)
- [32-bit Python Key Derivation Fallback (When `.NET 8 x86` Is Missing)](#32-bit-python-key-derivation-fallback-when-net-8-x86-is-missing)
- [Post-Extraction Image Formats (PNG / JPG / TLG / PSB-PIMG)](#post-extraction-image-formats-png--jpg--tlg--psb-pimg)
- [Other Common Community Tools](#other-common-community-tools)

## Identifying XP3 Protection Type

Run `game_extract.py detect <game_dir> --json` or `game_extract.py xp3-inspect <archive.xp3> --json`.

| Indicator                                                                               | Meaning                                                                  | Recommended path                                  |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------- |
| `has_hxv4: false`, `protected_count: 0`                                                 | Standard unencrypted XP3 (v1 or v2)                                      | Path A (`xp3-unpack` + `convert-raw`)             |
| `has_hxv4: true` + `protected_count > 0` + dummy `\u5000`/`\u5001` names + `.sig` files | Modern Yuzusoft / KiriKiriZ `bres://` + `BOOTSTRAP` + `Hxv4` + `CxdecV2` | Path B (offline static recovery)                  |
| `has_hxv4: false`, `protected_count > 0`                                                | Classic `cxdec` / XOR / game-specific XP3 encryption                     | Check GARbro (maintained forks) or `arc_unpacker` |

Notice: entry #0 in protected XP3s is commonly an unprotected plaintext stub (`Warning: Extracting this...`), while real entries have `flags & 0x80000000` (`TVP_XP3_FILE_PROTECTED`).

## Path A: Unencrypted / Standard XP3

Unpack directly with `game_extract.py` (supports XP3 v1 & v2, compressed/uncompressed indices and segments, and strips path traversal):

```bash
uv run --quiet --python 3.12 python -I path/to/game_extract.py xp3-unpack "<game_dir>/bgimage.xp3" -o "<out_dir>/raw/bgimage"
```

Then convert any `.tlg` or `.pimg`/`.psb` files with `convert-raw` and verify with `verify`.

## Path B: Encrypted `Hxv4` / `CxdecV2` XP3 (Offline Static Recovery)

For modern KiriKiriZ titles using the `bres://` + `BOOTSTRAP` + `Hxv4` + `CxdecV2` chain (with `STARTUP.TJS` and `BOOTSTRAP` embedded in the game's PE resources), use the offline static toolkit [`hktkqj/cxdec-hxv4-static-analysis`](https://github.com/hktkqj/cxdec-hxv4-static-analysis). It never launches the game or attaches a debugger.

### 1. Clone the Static Toolkit into Task Temp

```bash
git clone --depth 1 https://github.com/hktkqj/cxdec-hxv4-static-analysis.git "<tmp>/cxdec-hxv4-static-analysis"
```

### 2. Probe PE Resources (`--skip-derive`)

```powershell
$repo = "<tmp>\cxdec-hxv4-static-analysis"
$work = "<tmp>\recover"
$exe  = "<game_dir>\<game>.exe"

uv run --python cpython-3.12-windows-x86_64-none --with pycryptodome --with pillow python "$repo\src\static_extract\static_xp3_recover.py" --exe $exe --work-dir $work --skip-derive --debug
```

Successful output recovers:

- `$work\STARTUP.TJS` (decompiled) and `bootstrap_prefix`
- `$work\bootstrap.dll` (decompressed 32-bit internal DLL)
- `$work\static_recover.summary.json` (including `bootstrap_prefix` and `archive_unique_key`)

### 3. Derive `drip_program.json` (32-bit x86 Required)

The recovered `bootstrap.dll` is a 32-bit PE DLL, so key derivation must run in a 32-bit process:

- **If `.NET 8 x86` SDK/runtime is installed** (`C:\Program Files (x86)\dotnet\dotnet.exe`): run `static_xp3_recover.py` without `--skip-derive`.
- **If `.NET 8 x86` is NOT installed** (common on 64-bit Windows machines): use the 32-bit Python fallback below (`uv run --python cpython-3.12-windows-x86-none`).

### 4. Verify Checksums Before Full Extraction (Use 64-bit Python!)

Always use **64-bit Python** (`cpython-3.12-windows-x86_64-none`) for `xp3_inspect.py` because it reads entire `.xp3` archives (500 MB–2 GB) into memory and will hit `MemoryError` under 32-bit Python:

```powershell
$targets = @("image.xp3", "uipsd.xp3", "bgimage.xp3", "fgimage.xp3", "evimage.xp3", "evimage2.xp3", "patch.xp3") | ForEach-Object { Join-Path "<game_dir>" $_ }

uv run --python cpython-3.12-windows-x86_64-none --with pycryptodome --with pillow python "$repo\src\common\xp3_inspect.py" verify @targets --filter recovered --drip-program "$work\drip_program.json" --max-entries 10
```

Confirm `failed=0 unresolved_filter=0` for every archive before extracting.

### 5. Extract All Image Archives & Scan Headers

```powershell
$outRaw = "<out_dir>\raw"
uv run --python cpython-3.12-windows-x86_64-none --with pycryptodome --with pillow python "$repo\src\common\xp3_inspect.py" extract-all $outRaw @targets --filter recovered --drip-program "$work\drip_program.json"

uv run --python cpython-3.12-windows-x86_64-none python "$repo\tools\scan_headers.py" -i $outRaw -o "$outRaw\file_type_report.txt" --layout flat
```

### 6. Convert Raw `.bin` Files to PNG/JPG & Verify (Metadata-Only)

```powershell
uv run --python cpython-3.12-windows-x86_64-none --with pillow python -I path/to/game_extract.py convert-raw $outRaw -o "<out_dir>\images" --tlg2png "$repo\tools\tlg2png\tlg2png.exe" --psb-parser "$repo\tools\psb_parser.py"

uv run --python cpython-3.12-windows-x86_64-none python -I path/to/game_extract.py verify "<out_dir>\images" --json
```

## 32-bit Python Key Derivation Fallback (When `.NET 8 x86` Is Missing)

When `C:\Program Files (x86)\dotnet\dotnet.exe` is unavailable (or `.NET Framework` binaries hit `0x8007045A` in a non-interactive shell), `uv` can fetch a 32-bit Python (`cpython-3.12-windows-x86-none`) on the fly and call the 4 derivation RVAs in `bootstrap.dll` directly via `ctypes`:

1. Load `bootstrap.dll` with `kernel32.LoadLibraryW`.
2. Wrap the three `__thiscall` RVAs (`ManagerCtor = 0x0E2D0`, `BootstrapDerive = 0x15630`, `ArchiveDerive = 0x157D0`) with a 10-byte `VirtualAlloc(PAGE_EXECUTE_READWRITE)` stdcall→thiscall trampoline:
   ```text
   58              ; pop eax        (return address)
   59              ; pop ecx        (this -> ECX)
   50              ; push eax       (restore return address)
   B8 <target32>   ; mov eax, target
   FF E0           ; jmp eax
   ```
3. Call `HashKeyDerive` (`0x10410`) directly via `ctypes.CFUNCTYPE` (`__cdecl`).
4. Read `TableRva` (`0x80E38`), `ArchiveSeedRva` (`0x81758`, with fallback scan in `ArchiveDerive`), `hxv4_key` (`manager + 0x3040`, 32 bytes), `hxv4_nonce0` (`manager + 0x3080`, 24 bytes), `hxv4_nonce1` (`manager + 0x3060`, 24 bytes), `holder_words`, `context_u32`, and all 128 `lanes` matching `tools/FilterManagerDerive/Program.cs`, and write `drip_program.json`.

Always compare RVAs against the target repo's `tools/FilterManagerDerive/Program.cs` before running on a new game family.

> **Windows `uv` cleanup tip:** After using `cpython-3.12-windows-x86-none`, remove it with `uv python uninstall cpython-3.12-windows-x86-none` (or install the matching 64-bit patch version) so plain `uv run --python 3.12` does not pick 32-bit Python for 64-bit-only wheels like `scipy` or large `UnityPy` / XP3 jobs.

## Post-Extraction Image Formats (PNG / JPG / TLG / PSB-PIMG)

Decrypted `Hxv4` entries are written as `.bin` files because original filenames are hashed. Always classify by magic bytes rather than filename extension:

| Magic bytes (hex)            | Format                    | Handling                                                                                                                                                           |
| ---------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `89 50 4E 47 0D 0A 1A 0A`    | PNG                       | Copy directly as `.png`                                                                                                                                            |
| `FF D8 FF`                   | JPEG                      | Copy directly as `.jpg`                                                                                                                                            |
| `54 4C 47 30` / `35` / `36`  | TLG0 / TLG5 / TLG6        | Convert with `tlg2png.exe <in> <out.png>`                                                                                                                          |
| `50 53 42 00`                | PSB / PIMG                | Run `psb_parser.py compose-all` (composes base + diff layers into full CG PNGs); fallback to `extract-all --png`; skip if no embedded images (scenario/motion PSB) |
| `FE FE ...` / `54 4A 53 ...` | Scrambled UTF-16LE / TJS2 | Script/metadata, not an image                                                                                                                                      |

Remember: `.pimg` CG packs often expand into 10–30 full-resolution 1920×1080 PNG variations per archive entry (e.g., 108 `.pimg` files → ~3,000 PNGs / ~7 GB). Ensure sufficient disk space before converting.

## Other Common Community Tools

- **GARbro** (maintained community forks such as `crskycode/GARbro`): Windows GUI browser/extractor supporting many classic KiriKiri encryption schemes and `.tlg`/`.pimg` conversion; Hxv4 games also require filename hash lists (`HxNames.lst`) if human-readable names are desired.
- **`arc_unpacker`**: Open-source CLI extractor for standard and many older encrypted visual novel archives.
- **`MLChinoo/hxv4_unhash_tools`**: Optional hash-collision dictionary tool for recovering human-readable filenames (`HxNames.lst`) from Hxv4 archives when needed.
