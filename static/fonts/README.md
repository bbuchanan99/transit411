# Fonts embedded in exported data cards

Archivo and JetBrains Mono, both under the SIL Open Font License 1.1, taken from Google Fonts
(latin subset, only the weights the card uses).

They are here because a PNG export rasterises the card's SVG: web fonts referenced by the page
are not available to that raster, so the files are inlined as base64 at export time. Without
them the export still works, in Helvetica/Arial and a system monospace.

- Archivo: https://fonts.google.com/specimen/Archivo (OFL 1.1)
- JetBrains Mono: https://fonts.google.com/specimen/JetBrains+Mono (OFL 1.1)
