# What it changes and what it leaves

From the Coloring Helper README by staryume, with the agent-facing consequences.

## Changes

- Fills strips left between paint and line.
- Removes paint that crosses a line.
- Fills pinholes and small unpainted pockets that lines seal off (amber in the review PNG).
- Runs every colour to the middle of its line, so paint meets under the ink.
- Makes soft (semi-transparent) paint solid, but only beside a line, where paper would show
  through.

## Leaves alone

- A paint edge stays only on a line, or where two wide areas of paint meet. Whole areas move, so no
  new edge appears away from a line.
- A thin tip belongs to whatever sits at its wide end: a bang or strand tip between two lines takes
  the strand's colour, and the narrow end of a gap between strands stays empty.
- Paint that fills a channel from line to line can change colour but is never erased.
- A shape painted without any line around it (a blush, a soft shadow) is left alone, as is
  everything under a protect layer or box.
- An unpainted strand whose lines stay open to the background is left unpainted. A thin unpainted
  strand closed by its lines is filled and shown in amber.

## Cannot fix

- Wide block-in errors where no selected line separates the colours. Add the missing outline
  layer to `lineart`, or tell the user these need a hand fix.
- Anything outside the 8-bit RGB, plain-flats model: CMYK, 16-bit, flats with masks, clipping, or
  layer effects. The script stops with a message naming the layer.

## Output

- `<name>-polished.psd`: a full copy. Each changed flat appears as `<flat> (polished)` directly
  above the original, which is hidden. Untouched flats and every other layer are unchanged. The
  ICC profile and canvas size are checked after saving.
- `<name>-polished-review.png`: before, after, and after with mint (edited) and amber (filled
  pocket, inspect) overlays, side by side at full size.
- Both files appear together or not at all: they are written to a temporary folder next to the
  output and hard-linked into place, refusing to replace existing files.
