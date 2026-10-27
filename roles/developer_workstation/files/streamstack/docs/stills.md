# Stills: AVIF and JPEG XL

`streamstack stills` and `streamstack.stills` write still pictures in the formats that give equal quality in fewer
bytes. None of it is a real-time path (about 0.6 s for a 1080p frame).

| Command | What it does |
|---|---|
| `streamstack stills status [--json]` | What this host can write: the AV1 encoder for AVIF, and which libjxl does the JPEG recompression. |
| `streamstack stills snapshot <image> [--output f.avif] [--quality 28]` | Writes any image as AVIF (ffmpeg with `libsvtav1`, `libaom-av1` or `librav1e`). Quality is the AV1 CRF, 0 to 63, lower is better. AVIF opens in every current browser and phone. |
| `streamstack stills archive <jpeg>... [--remove-original]` | Recompresses each Canon (or any) JPEG to JPEG XL, **reversibly**, and proves it before keeping it. |
| `streamstack stills restore <jxl>...` | Rebuilds the original JPEG file from an archive. |

## Lossless JPEG recompression

A JPEG XL file made from a JPEG keeps the JPEG's DCT coefficients and the data needed to rebuild the original file.
No pixels are decoded and re-encoded, so nothing is lost and the file is typically about 20 percent smaller (the test
picture here: 24 699 to 18 212 bytes, 26 percent; a 2.9 MB noisy picture: 6 percent).

`archive` is deliberately paranoid. It writes the `.jxl` to a temporary name, rebuilds the JPEG from that file and
compares it **byte for byte** with the original. Only a match renames it into place; a mismatch deletes it and keeps
the JPEG. `--remove-original` deletes the JPEG only after that proof. Nothing is ever overwritten, and a half-written
file is never left behind. The result line carries the SHA-256 of the original, which `restore` reproduces.

The round trip is tested in CI against the pinned `imagecodecs` wheel and, where present, the system libjxl, on a
camera-style JPEG and on a multi-megabyte noisy one (`tests/test_stills.py`).

## What it needs

* AVIF: ffmpeg with an AV1 encoder (any common build).
* JPEG recompression: one libjxl. Either `pip install 'streamstack[stills]'` (the `imagecodecs` wheel bundles it) or
  the system package (`libjxl0.11`, used through ctypes). `stills status` says which one is in use and, when neither
  is there, how to get it. Progressive or arithmetic-coded JPEGs may be refused by libjxl; the error says so and the
  file is left alone.

## Not done here

JPEG XL for pixels that did not come from a JPEG (the lossy or lossless "archive of a PNG") is not offered: the
reversible path above is the one with a measured benefit for the Canon files, and `ffmpeg -c:v libjxl` already
covers the rest. The Console does not save stills by itself yet; the commands work on any files, including the ones
the Canon bridge or a screenshot writes.
