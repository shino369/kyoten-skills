# Unity Image Extraction Reference

Detailed notes for extracting images from Unity games (`*_Data`, `.assets`, Addressables `.bundle`, Naninovel, and AssetRipper).

## Table of Contents

- [Directory Layout](#directory-layout)
- [UnityPy Extraction (Recommended for Images)](#unitypy-extraction-recommended-for-images)
- [AssetRipper 2.0+ (Full Project / Audio / Video / GUI)](#assetripper-20-full-project--audio--video--gui)
- [Layered 2D Characters & SpriteAtlases](#layered-2d-characters--spriteatlases)
- [Windows Gotchas](#windows-gotchas)

## Directory Layout

Typical Unity PC builds (Mono or IL2CPP):

```text
<game_dir>/
├── <game>.exe
├── GameAssembly.dll               # IL2CPP build
└── <game>_Data/
    ├── resources.assets           # Root Unity assets
    ├── globalgamemanagers.assets
    ├── sharedassets0.assets       # Splash/logo sprites often live here
    └── StreamingAssets/
        └── aa/                    # Unity Addressables
            └── StandaloneWindows64/
                ├── *.bundle
                └── <category_subdirs>/*.bundle
```

Before extracting, group `.bundle` files by prefix/subfolder and check sizes. In visual novels (especially Naninovel titles), 70–85% of disk size is usually voice/BGM/video bundles (`general-voice-*`, `naninovel-audio*`, `*-movies*`). Skipping those makes image extraction 5–10x faster and avoids writing gigabytes of unwanted audio/video.

## UnityPy Extraction (Recommended for Images)

`UnityPy` uses `tpk-ar` (AssetRipper's TypeTree package) plus `texture2ddecoder`, `etcpak`, and `astc-encoder-py`, so it decodes DXT1/DXT5 Crunched (`28`/`29`), BC7 (`25`), BC4/BC5 (`26`), RGB24 (`3`), RGBA32 (`4`), ETC, and ASTC directly in Python via `uv`:

```powershell
uv run --python 3.12 --with UnityPy --with pillow python -I path/to/game_extract.py unity-extract "<game_dir>/<name>_Data" -o "<out_dir>"
```

Output folder layout produced by `game_extract.py unity-extract`:

| Folder                      | Contents                                                                                                     |
| --------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `Backgrounds/`              | Full-resolution `Texture2D` backgrounds/CGs, preserving original Unity container subpaths                    |
| `Characters/<char>/sheets/` | Full `Texture2D` sprite sheets (e.g., 4096×8192 or 8192×8192 BC7)                                            |
| `Characters/<char>/parts/`  | Individually cropped `Sprite` layers (body, arms, eyes, mouths, expressions)                                 |
| `Spawn_Events_CutIns/`      | `textures/` plus `cropped_sprites/` (1:1 duplicate sprites matching texture name+size are skipped)           |
| `UI_Sprites/`               | Cropped `Sprite` PNGs extracted out of `SpriteAtlas`es (`sactx-*` atlases skipped), plus standalone textures |
| `Other/`                    | Remaining non-skipped bundles and root `.assets` (`RootAssets/`)                                             |

## AssetRipper 2.0+ (Full Project / Audio / Video / GUI)

Use [AssetRipper](https://github.com/AssetRipper/AssetRipper) when you need:

- Audio (BGM, voices, SFX), video (`.mp4`/`.webm`), scripts, prefabs, or a full Unity project export
- Interactive browsing of asset references in a browser

### Desktop GUI Workflow

1. Download the latest `AssetRipper_win_x64.zip` (or OS-matching build) from GitHub Releases and extract it.
2. Launch `AssetRipper.GUI.Free.exe` — it opens a local Web UI (`http://127.0.0.1:<port>`).
3. Choose **File → Open Folder** and select `<game_dir>/<name>_Data`.
4. Wait for loading to complete (watch the console window for progress).
5. Choose **Export → Export All Files**, pick an output folder, and click **Export Primary Content** (produces `Assets/` with PNGs, audio, video, etc.).

### Headless CLI & HTTP API (AssetRipper 2.0+)

AssetRipper 2.0+ exposes Posix CLI flags and an OpenAPI HTTP server:

```bash
AssetRipper.GUI.Free --headless --port 54321 --log-path assetripper.log
```

Key endpoints (`application/x-www-form-urlencoded` with `Path=...`; multiple `Path` fields supported):

- `POST /LoadFolder` — load one or more folders
- `POST /LoadFile` — load individual `.bundle` / `.assets` files
- `POST /Export/PrimaryContent` — export decoded primary assets
- `POST /Export/UnityProject` — export a full Unity project
- `POST /Reset` — unload current files

## Layered 2D Characters & SpriteAtlases

- **Naninovel `LayeredCharacter` prefabs** (and Spine/Live2D rigs) are composed at runtime from many `SpriteRenderer` layers rather than stored as a single static standing image. `unity-extract` saves both the full atlas sheet (`sheets/`) and every cropped part (`parts/`). Assembling a complete pose requires reading the prefab's `Transform` / `SpriteRenderer` hierarchy (layer positions and sorting order).
- **`SpriteAtlas` (`sactx-*`)** packs dozens of UI elements into one texture. Extracting `Sprite` objects via `UnityPy` (`sprite.image`) cleanly cuts out every individual UI asset with its original container name, whereas raw `Texture2D`-only export yields packed sheets.

## Windows Gotchas

- **`AssetRipper.GUI.Free.exe` exits with `-1073741502` (`0xC0000142` / `STATUS_DLL_INIT_FAILED`) in non-interactive shells.** Single-file GUI binaries may fail DLL init inside headless/non-interactive agent terminals even when they work fine from the user's desktop. Fall back to `UnityPy` (`game_extract.py unity-extract`) for automated image extraction, and leave `AssetRipper.GUI.Free.exe` for desktop launch if the user wants audio/video/full-project export.
- **PowerShell 5.1:** Does not support `&&` as a command separator; use newlines with `if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }` or `;`.
- **Use 64-bit Python:** Large 8192×8192 BC7 textures and multi-hundred-MB bundles require 64-bit Python (`--python cpython-3.12-windows-x86_64-none` if a 32-bit interpreter is also cached by `uv`).
