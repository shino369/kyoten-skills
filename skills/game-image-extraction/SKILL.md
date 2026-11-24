---
name: game-image-extraction
description: Extract, convert, and verify image assets (CGs, backgrounds, character sprites, cut-ins, UI atlases, portraits) from local Unity games (`*_Data`, `.assets`, Addressables `.bundle`, Naninovel, LayeredCharacter, AssetRipper) and KiriKiri2/KiriKiriZ visual novels (`.xp3`, plain or `Hxv4`/`CxdecV2` encrypted, `.tlg` TLG5/TLG6, `.psb`/`.pimg` layered CGs). Use whenever the user asks to extract, unpack, rip, dump, or inspect images/CGs/sprites from a Unity game, Steam game, visual novel, galgame, `.bundle`, `.assets`, `.xp3`, `.tlg`, or `.pimg` file, or mentions AssetRipper, UnityPy, KiriKiri, GARbro, or Hxv4, even if they do not name a specific tool. Supports a strict metadata-only verification mode that never opens or reads extracted images when content may be sensitive or NSFW.
---

# Game Image Extraction (Unity & KiriKiri2/Z)

Extract image assets from local Unity and KiriKiri2/KiriKiriZ games cleanly into PNG/JPG folders without modifying the game installation directory.

One cross-platform CLI script, `scripts/game_extract.py`, handles engine detection, Unity extraction, plain XP3 inspection/unpacking, raw `.bin` conversion (PNG/JPG/TLG/PSB-PIMG), and metadata-only verification. Run it through `uv` (no global installs):

PowerShell:

```powershell
$GE_PY = Join-Path (git rev-parse --show-toplevel) '.agents/skills/game-image-extraction/scripts/game_extract.py'
function ge { uv run --quiet --python 3.12 --with UnityPy --with pillow python -I $GE_PY @args }
```

bash / zsh / Git Bash:

```bash
GE_PY="$(git rev-parse --show-toplevel)/.agents/skills/game-image-extraction/scripts/game_extract.py"
ge() { uv run --quiet --python 3.12 --with UnityPy --with pillow python -I "$GE_PY" "$@"; }
```

When running only KiriKiri commands (`detect`, `xp3-inspect`, `xp3-unpack`, `verify`), `UnityPy` and `pillow` are not required. For `convert-raw` with `--psb-parser`, include `--with pillow` so `psb_parser.py` can compose `.pimg` layers.

## Safety & Content-Sensitivity Rules

1. **Read-only on the game folder.** Never modify, overwrite, or write scratch files inside the game's Steam/install folder. Write all outputs to the active task folder (e.g., `tasks/<task>/extracted/`) or a user-chosen output directory.
2. **Respect metadata-only mode (do not read/view images).** Visual novels and galgames may contain mature/NSFW artwork. Whenever the user asks not to view/evaluate extracted images — or when the target is an adult/R-18 visual novel — **never** call image-reading tools on extracted files. Verify extraction exclusively via `ge verify <out_dir>` (file counts, zero-byte checks, PNG/JPEG magic headers) and manifest/report JSON files.
3. **Prefer offline static extraction.** Never execute untrusted DLL injectors or attach debuggers to a running game unless the user explicitly asks and understands the risk. Both Unity (`UnityPy`) and KiriKiri Hxv4/CxdecV2 (`static_xp3_recover`) work completely offline.
4. **Never bypass DRM or access controls.** Only unpack standard or user-owned local game asset archives; stop and report honestly if a game uses an unsupported DRM wrapper.

## Workflow

### Step 1: Detect Engine & Layout (Read-Only)

```bash
ge detect "<game_dir>" --json
```

Inspect the JSON output and branch by engine:

| Detected engine                   | Indicators                                           | Next reference to read                                                              |
| --------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `unity`                           | `*_Data/`, `StreamingAssets/aa/*.bundle`, `*.assets` | [references/unity.md](references/unity.md)                                          |
| `kirikiri` (plain)                | `*.xp3`, `has_hxv4: false`, `has_protected: false`   | Run `ge xp3-unpack`, then [references/kirikiri.md](references/kirikiri.md)          |
| `kirikiri` (`Hxv4` / `protected`) | `*.xp3`, `has_hxv4: true` or `protected_count > 0`   | [references/kirikiri.md](references/kirikiri.md) (offline static Hxv4/CxdecV2 flow) |
| `unknown`                         | No `*_Data` or `*.xp3`                               | List top-level files/sizes and ask the user or identify the engine                  |

Also check free disk space before extracting large archives (uncompressed PNGs and composed `.pimg` variations can be 2x–4x larger than the source archives).

### Step 2: Extract Images

#### Unity (`unity-extract`)

```bash
ge unity-extract "<game_dir>/<name>_Data" -o "<out_dir>"
```

- Skips voice, BGM, movie, font, and localization-script bundles by default (often 70–85% of game size). Pass `--include-all` to scan every bundle.
- Extracts both `Texture2D` assets (full backgrounds, sheets, portraits) and cropped `Sprite` assets (individual UI sprites from `SpriteAtlas`, character parts from `LayeredCharacter` prefabs, cut-ins) while deduplicating exact 1:1 `Sprite`/`Texture2D` pairs.
- Writes `<out_dir>/manifest.json` with counts and any decode errors.
- Need full Unity project export, audio, video, or AssetRipper GUI/HTTP API? See [references/unity.md](references/unity.md).

#### KiriKiri2 / KiriKiriZ (`xp3-inspect`, `xp3-unpack`, `convert-raw`)

1. Inspect an archive header first:

   ```bash
   ge xp3-inspect "<game_dir>/image.xp3" --json
   ```

2. **Plain / unencrypted XP3** (`has_hxv4: false`, `protected_count: 0`):

   ```bash
   ge xp3-unpack "<game_dir>/image.xp3" -o "<out_dir>/raw/image"
   ```

3. **Encrypted `Hxv4` / `CxdecV2` XP3** (`has_hxv4: true` or `protected_count > 0`):
   Follow the offline static recovery workflow in [references/kirikiri.md](references/kirikiri.md) (`static_xp3_recover.py` + 32-bit key derivation + `xp3_inspect.py extract-all` into `<out_dir>/raw/`).
4. **Convert extracted raw files into PNG/JPG** (handles PNG, JPEG, TLG0/5/6 via `tlg2png`, and PSB/PIMG layered CGs via `psb_parser.py`):

   ```bash
   ge convert-raw "<out_dir>/raw" -o "<out_dir>/images" \
     --tlg2png "<path/to/tlg2png.exe>" \
     --psb-parser "<path/to/psb_parser.py>"
   ```

   - Non-image PSB files (scenario/motion scripts) are skipped cleanly without failing the run.
   - Writes `<out_dir>/images/convert_summary.json`.

### Step 3: Verify Output

Always run metadata-only verification first:

```bash
ge verify "<out_dir>" --json
```

Check:

- `invalid == 0` and `zero_byte == 0`
- `manifest.json` / `convert_summary.json` reports `error_count == 0`
- Only if the content is safe AND the user did not forbid viewing images, optionally spot-check 2–3 sample PNGs (e.g., one background, one UI sprite, one portrait) to confirm upright orientation, alpha transparency, and colors.

### Step 4: Report Clearly

Report to the user:

- Detected engine, archive protection type, and extraction method used.
- Output directory path and per-category image counts + total size.
- Manifest/verification status (confirming metadata-only verification when image viewing was skipped).
- Any optional cleanup (e.g., removing intermediate `raw/` folders to reclaim disk space) or follow-up options (e.g., compositing Unity `LayeredCharacter` prefabs, or running AssetRipper desktop GUI for audio/video).

## Tests

When developing inside the skill repository, run the Python test suite with 100% branch coverage:

```bash
uv run --quiet --python 3.12 --with pytest --with pytest-cov --with pillow python -I -B -m pytest -q -p no:cacheprovider skills/game-image-extraction/scripts --cov=game_extract --cov-branch --cov-fail-under=100
```
