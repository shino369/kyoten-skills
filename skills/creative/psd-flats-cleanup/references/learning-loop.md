# Learning from a file the artist finished by hand

`pfc compare Art.psd Art-finished.psd [--output diff.png]` reruns the tool on the source with the
saved settings and scores it against a finished copy: a polished PSD whose `(polished)` flats the
artist corrected by hand (or the original flats, if a flat was not polished).

It prints:

- **matches N%**: of the pixels the artist changed between the lines, the share the tool
  reproduces.
- **changed N pixels that the artist left alone**: over-corrections.
- **N pixels still differ** and the top colour pairs (`tool: Hair   artist: Skin`), naming where
  the disagreement is.

`Art-compare.png` shows the tool's flats on the left and the artist's on the right, with lineart
darkened and every differing pixel in red. A flat the artist hid or removed in the finished file
is not scored. Both PSDs must have the same canvas size.

On staryume's 4096 px sample the tool reproduces 86.6% of 91,941 corrected pixels, changes 3,477
that were left alone, and 15,802 still differ.

## Using it

- Report the three numbers and the top pairs to the user. Pairs point at the fix: a missing
  outline layer, a protect box, or a different `max_region_px`.
- Keep every source and finished pair. When trying another `max_region_px` or different roles, a
  change is worth keeping only if the numbers improve on every pair.
- Do not change `correction_map` in the script to fit one file. The rules are shared by every
  artwork, and the tests pin them.
