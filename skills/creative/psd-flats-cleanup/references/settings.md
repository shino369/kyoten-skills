# Settings file (`<name>.cleanup.json`)

`setup` writes it next to the PSD. Keep it for reruns. It records each chosen layer by its
position and its names, so a moved, renamed, or hidden layer stops `polish` with
`Layer moved, changed name, or became hidden`. Move the old file to a `backup/` folder and run
`setup` again.

```json
{
  "layers": {
    "lineart": [
      { "index": [1, 1, 0], "names": ["Drawing", "Contours", "Ink"] }
    ],
    "flat": [{ "index": [1, 0, 0], "names": ["Drawing", "Paint", "Hair"] }],
    "background": [{ "index": [0], "names": ["Paper"] }],
    "protect": []
  },
  "protect_boxes": [],
  "max_region_px": 40,
  "ignore": [{ "index": [3], "names": ["Notes"] }]
}
```

`index` is the path of positions from the document root (0 = bottom), `names` the matching layer
and group names. Do not edit the file by hand: `setup` writes every key from flags, checked
against the PSD. To change a setting, move the file to a `backup/` folder and run `setup` again.
`pfc settings Art.psd` prints what `polish` will apply, with names escaped; `polish` refuses a file
with unknown keys or values of the wrong type.

## Roles

| Role         | Required | What to choose                                                                                                     |
| ------------ | -------- | ------------------------------------------------------------------------------------------------------------------ |
| `lineart`    | yes      | Outline layers that separate the flats. Leave out hatching and construction lines: every selected stroke is a wall |
| `flat`       | yes      | The paint to repair: Normal, 100% opacity and fill, no mask/clipping/effects on it or its groups                   |
| `background` | no       | Paper or backdrop, below every flat. A trimmed pixel shows it again (or transparency)                              |
| `protect`    | no       | Paint and details that must stay exactly as they are (eye whites, highlights painted on their own layer)           |

Each layer can have one role. Light-coloured ink on a transparent layer still counts as a line;
on an opaque Multiply scan, white paper counts as no line.

## Other settings (setup flags)

- **`--protect-box left,top,right,bottom`** (`protect_boxes`, repeatable): a rectangle in
  full-size whole-pixel coordinates, inside the canvas. Bare canvas inside a box stays bare (use
  it for highlights left unpainted); paint inside a box can still be corrected. Read coordinates
  off the review PNG: its panels are the canvas at full size below a header strip, so subtract the
  header height from `y` and, for the second and third panels, one or two canvas widths from `x`.
- **`--max-region-px N`** (`max_region_px`): an integer from 8 to 128. Default: 1% of the shorter
  canvas side, clamped to 8–64. A painted or unpainted area narrower than this, and partly walled
  in by lines, joins the wider area it opens into. Raise it for bigger block-in errors; lower it if
  small correct shapes get absorbed. Change it in small steps (for example 40 → 56) and compare
  the review PNGs.
- **`--ignore N,N`** (`ignore`): layer numbers from `pfc layers` to hide in the output copy, such
  as notes or guides. Only raster (pixel) layers are listed and can be ignored; ask the user to
  hide a text or shape notes layer in their app. A layer with a role cannot also be ignored.
