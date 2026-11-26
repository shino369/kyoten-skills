---
name: transparent-background
description: Turn an image with a flat or near-flat background (illustration, sticker, logo, scan, product shot on white) into a clean transparent PNG, then fix what is left — white halo or jagged edges, leftover background pixels between hair strands or inside outlines — and optionally recolour separate shapes (marks, text, icons) to a given or sampled colour. Use when the user asks to remove a background, make an image transparent, cut out a character or logo, clean edges or white fringes of a cutout, fill leftover white pixels, or change the colour of part of an image.
---

# Transparent Background

One script, `scripts/imgalpha.py`, does the work. Run it through uv (no global installs) on
macOS, Linux, or Windows, with the pinned versions below. Define the `ia` helper in each shell
command that uses it; the examples below use it. Set `IA_PY` to the absolute path of
`scripts/imgalpha.py` inside the folder that holds this SKILL.md (Claude Code shows it as the
skill's base directory; a project install is `<workspace>/.agents/skills/transparent-background/`).
Never derive it from the current directory: a repository you are inside could hold its own copy.

**Run `ia` from the workspace root or another folder you trust, never from inside a folder of
received images.** uv reads `pyproject.toml`, `uv.toml`, and `.venv` from the current folder and its
parents, so a folder that came with the images could change what gets installed and run.
`--no-project --no-config` keeps the first two out; the folder rule covers the rest. Pass images as
absolute paths or paths relative to that folder.

bash / zsh / Git Bash:

```bash
IA_PY="/absolute/path/to/transparent-background/scripts/imgalpha.py"
ia() { uv run --no-project --no-config --no-build --exclude-newer P7D --python 3.12 --with pillow==13.0.0 --with numpy==2.5.5 --with scipy==1.18.1 python -I "$IA_PY" "$@"; }
ia remove in.jpg -o out.png
```

PowerShell (`cmd.exe` has no functions, so use PowerShell or Git Bash):

```powershell
$IA_PY = 'C:\absolute\path\to\transparent-background\scripts\imgalpha.py'
function ia { uv run --no-project --no-config --no-build --exclude-newer P7D --python 3.12 --with pillow==13.0.0 --with numpy==2.5.5 --with scipy==1.18.1 python -I $IA_PY @args }
ia remove in.jpg -o out.png
```

`--no-build` installs wheels only (all three packages have them for Python 3.12 on macOS, Linux, and
Windows). `--exclude-newer P7D` makes uv ignore files uploaded in the last 7 days, so a file added
later to a pinned release is not picked up; it needs uv 0.9.17 or later (`uv --version`).
`--no-config` also skips your own uv settings, such as a private index.

Write colours as `r,g,b` (`--bg 40,96,200`). It needs no quoting in any shell. `#rrggbb` also
works, but quote it: PowerShell treats a bare `#` as a comment, and `cmd` keeps single quotes.

## Workflow

1. **Look first.** Read the image. This script suits flat or near-flat backgrounds. For photos or
   busy backgrounds use an AI matting model instead (`rembg`, see
   [references/troubleshooting.md](references/troubleshooting.md#when-not-to-use-this)).
2. **Never touch the original.** Write to a new file. Before editing a result in place, copy it to a
   `backup/<yymmdd-hhmm>/` folder next to it. The script refuses `-o` = input unless `--in-place`.
3. **Remove** the background: `ia remove in.jpg -o out.png`. The bg colour is the border median
   (`--bg 255,255,255` to force). It removes bg connected to the edges plus enclosed gaps of the bg's
   hue that have no hole, so skin, white stripes and ring-shaped details stay.
4. **Inspect on a contrasting colour, zoomed.** Transparency hides problems on white:

   ```bash
   ia preview out.png -o check.jpg --bg 40,96,200 --crop 600,140 --crop 260,1140,60
   ```

   Read `check.jpg`. Check a dark background too. Look for halos, stair-steps, holes in light
   details, leftover white.

5. **Fix what you see**, one issue at a time, re-inspecting after each. Symptom → fix table:
   [references/troubleshooting.md](references/troubleshooting.md).
6. **Optional extras:** leftover specks (below), recolouring (below).
7. **Report** the output path, what changed, what was deliberately kept, and the backup location.

## Leftover background pixels (`specks`)

White slivers between hair strands or inside dark outlines. Always mark before filling:

```bash
ia specks out.png --mark marked.jpg          # circles every candidate; prints the count
ia specks out.png -o fixed.png --keep 1237,1467   # fill; protect a deliberate sparkle
```

Read `marked.jpg` and check every circle. If one sits on real art (lacing, eye glint, sparkle), use
`--keep x,y` or a `--region x0,y0,x1,y1`. Then compare before and after with `preview --crop`. The
defaults are tuned for dark, neutral areas (black hair, outlines). For coloured areas raise
`--surround-max-sat`.

## Recolouring shapes (`components`, `recolor`)

```bash
ia components out.png                    # id, bbox, size, mean colour of each shape
ia recolor out.png -o red.png --at 200,640 --at 1400,500 --like other.png --like-at 1350,880
ia recolor out.png -o red.png --ids 2,3 --color 228,100,91
```

`--like` samples the colour and texture of the shape at `--like-at` in a reference image. Each
pixel keeps its light/dark deviation, so brush or crayon texture survives. Pick shapes with `--at`
(a point inside them). It is safer than ids, which change when the image changes.

## Rules

- Inspect the result yourself (read the preview image) after every step. Do not assume the
  defaults worked.
- Pass paths as arguments and run Python with `-I`. Never name a scratch file after a stdlib module
  (`inspect.py` shadowed numpy's import once).
- Keep scratch output in the task's `tmp/` or the system temp folder, not next to the user's
  images.

## Tests

When developing inside the skill repository, run the Python test suite with 100% branch coverage:

```bash
pnpm test:python:imgalpha
```
