"""Pure image helpers (numpy + Pillow): change detection, blank detection,
page cropping, overlap stitching and duplicate hashing.

All functions are deterministic and OS-independent, which keeps the heuristic
parts of the harness unit-testable.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image


def to_gray(img: Image.Image, downscale: int = 1) -> np.ndarray:
    g = img.convert("L")
    if downscale > 1:
        g = g.resize((max(1, g.width // downscale), max(1, g.height // downscale)), Image.BILINEAR)
    return np.asarray(g, dtype=np.int16)


def changed_fraction(a: Image.Image, b: Image.Image, pixel_threshold: int = 10, downscale: int = 2) -> float:
    """Fraction of pixels whose grey level differs by more than ``pixel_threshold``.

    Images of different size count as completely different (1.0).
    """
    if a.size != b.size:
        return 1.0
    ga, gb = to_gray(a, downscale), to_gray(b, downscale)
    return float(np.mean(np.abs(ga - gb) > pixel_threshold))


def gray_std(img: Image.Image) -> float:
    return float(np.std(to_gray(img, 2)))


def is_uniform(img: Image.Image, std_threshold: float = 2.0) -> bool:
    return gray_std(img) < std_threshold


def is_all_black(img: Image.Image) -> bool:
    g = to_gray(img, 4)
    return bool(g.size) and float(g.mean()) < 3.0 and float(g.std()) < 2.0


def sha256_image(img: Image.Image) -> str:
    h = hashlib.sha256()
    h.update(f"{img.mode}{img.size}".encode())
    h.update(img.tobytes())
    return h.hexdigest()


def dhash(img: Image.Image, size: int = 16) -> int:
    """Difference hash; robust to small rendering noise. 256 bits for size 16."""
    g = np.asarray(img.convert("L").resize((size + 1, size), Image.BILINEAR), dtype=np.int16)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# --------------------------------------------------------------------------- cropping

@dataclass
class CropResult:
    image: Image.Image
    box: tuple[int, int, int, int]   # (left, top, right, bottom) within the input
    fallback: bool
    canvas_color: Optional[tuple[int, int, int]]


def estimate_canvas_color(img: Image.Image) -> tuple[int, int, int]:
    """Most common colour along the outer 3-pixel border (the viewer's canvas)."""
    a = np.asarray(img.convert("RGB"))
    h, w, _ = a.shape
    b = 3
    border = np.concatenate([
        a[:b].reshape(-1, 3), a[-b:].reshape(-1, 3),
        a[:, :b].reshape(-1, 3), a[:, -b:].reshape(-1, 3),
    ])
    quant = (border // 4).astype(np.int32)
    keys = quant[:, 0] * 65536 + quant[:, 1] * 256 + quant[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    k = int(vals[np.argmax(counts)])
    sel = border[keys == k]
    return tuple(int(x) for x in sel.mean(axis=0))  # type: ignore[return-value]


def canvas_tolerance(canvas: tuple[int, int, int], tolerance: int) -> Optional[int]:
    """Colour tolerance for telling page from canvas, or None if they cannot be told apart.

    Newer Acrobat draws a very light grey canvas (around 240), close to page
    white, so the tolerance is narrowed to half the gap between canvas and
    white. Screen captures are lossless, so a few grey levels are reliable.
    A white canvas (>= 250) cannot be separated from a white page.
    """
    if min(canvas) >= 250:
        return None
    if min(canvas) >= 200:
        return max(2, min(tolerance, (255 - max(canvas)) // 2))
    return tolerance


def _page_mask(img: Image.Image, tolerance: int):
    """Boolean mask of "page" pixels, plus canvas colour and detection mode.

    * Dark or medium canvas (dark theme ~29, classic Acrobat grey ~82): the page
      is the *paper*, i.e. bright pixels. Dark UI on the canvas (side panels,
      tool strips, AI/message bars, scrollbars) is never mistaken for page.
    * Light canvas (newer Acrobat ~240): pixels that differ from the canvas,
      with a tolerance narrowed to half the gap to white.
    * White canvas: undecidable -> None.
    """
    rgb = np.asarray(img.convert("RGB"), dtype=np.int16)
    canvas = estimate_canvas_color(img)
    lum = sum(canvas) / 3.0
    if lum < 160:
        gray = rgb.mean(axis=2)
        return gray >= max(lum + 60.0, 150.0), canvas, "bright", rgb
    tol = canvas_tolerance(canvas, tolerance)
    if tol is None:
        return None, canvas, "undecidable", rgb
    diff = np.abs(rgb - np.array(canvas, dtype=np.int16)).max(axis=2) > tol
    return diff, canvas, "diff", rgb


def _longest_run(flags: np.ndarray, max_gap: int = 8) -> Optional[tuple[int, int]]:
    """Longest run of True values, bridging gaps of up to ``max_gap``."""
    idx = np.where(flags)[0]
    if idx.size == 0:
        return None
    best, start, prev = None, int(idx[0]), int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > max_gap + 1:
            if best is None or prev - start > best[1] - best[0]:
                best = (start, prev)
            start = i
        prev = i
    if best is None or prev - start > best[1] - best[0]:
        best = (start, prev)
    return best[0], best[1] + 1


def find_page_box(img: Image.Image, tolerance: int = 14):
    """Locate the page in a viewport capture.

    Columns are found first (the longest run of columns that are mostly page),
    then rows *within those columns*. Thresholds are relative to the strongest
    column/row, so partial-height strips or partial-width bars are ignored.
    Returns (box, canvas, mode) with box = (left, top, right, bottom) or None.
    """
    mask, canvas, mode, _ = _page_mask(img, tolerance)
    if mask is None:
        return None, canvas, mode
    col_share = mask.mean(axis=0)
    if float(col_share.max()) <= 0.05:
        return None, canvas, mode
    cols = _longest_run(col_share > max(0.05, 0.5 * float(col_share.max())))
    if cols is None:
        return None, canvas, mode
    c0, c1 = cols
    row_share = mask[:, c0:c1].mean(axis=1)
    if float(row_share.max()) <= 0.05:
        return None, canvas, mode
    rows = np.where(row_share > max(0.05, 0.3 * float(row_share.max())))[0]
    return (c0, int(rows[0]), c1, int(rows[-1]) + 1), canvas, mode


def crop_to_page(img: Image.Image, tolerance: int = 14, padding: int = 4,
                 min_page_fraction: float = 0.15) -> CropResult:
    """Crop the viewport capture to the rendered page (see :func:`find_page_box`).

    If the page cannot be located, or the detection is implausibly small, the
    full viewport is returned with ``fallback=True``.
    """
    w, h = img.size
    box, canvas, _ = find_page_box(img, tolerance)
    if box is None:
        return CropResult(img, (0, 0, w, h), True, canvas)
    left, top, right, bottom = box
    if (bottom - top) * (right - left) < min_page_fraction * w * h:
        return CropResult(img, (0, 0, w, h), True, canvas)
    box = (max(0, left - padding), max(0, top - padding), min(w, right + padding), min(h, bottom + padding))
    full = box == (0, 0, w, h)
    return CropResult(img.crop(box), box, full, canvas)


# --------------------------------------------------------------------------- stitching

@dataclass
class StitchMatch:
    offset: int          # row in `prev` where `new` row 0 lands
    mean_diff: float
    ambiguous: bool      # template had too little texture to be trusted
    accepted: bool


def find_overlap(prev: Image.Image, new: Image.Image, template_rows: int = 48,
                 max_mean_diff: float = 8.0, min_template_std: float = 4.0) -> StitchMatch:
    """Locate where the top of ``new`` appears inside ``prev`` (vertical scroll).

    Several templates from the top part of ``new`` are tried and the one with
    the most texture is used, so a blank band at the top does not make the
    match ambiguous. Returns the row offset in ``prev``; ``offset == prev.height``
    means no overlap was found.
    """
    gp = to_gray(prev).astype(np.float32)
    gn = to_gray(new).astype(np.float32)
    if gp.shape[1] != gn.shape[1]:
        return StitchMatch(gp.shape[0], 255.0, True, False)
    hp, hn = gp.shape[0], gn.shape[0]
    t = min(template_rows, hn, hp)
    # Choose the most textured template window within the top half of `new`.
    best_start, best_std = 0, -1.0
    for start in range(0, max(1, hn // 2 - t), max(1, t // 2)):
        s = float(gn[start:start + t].std())
        if s > best_std:
            best_start, best_std = start, s
    tmpl = gn[best_start:best_start + t]
    ambiguous = best_std < min_template_std
    best_y, best_d = None, float("inf")
    # Candidate positions: template must lie fully inside prev.
    for y in range(0, hp - t + 1):
        d = float(np.mean(np.abs(gp[y:y + t] - tmpl)))
        if d < best_d:
            best_d, best_y = d, y
            if d == 0.0:
                break
    if best_y is None:
        return StitchMatch(hp, 255.0, True, False)
    offset = best_y - best_start
    accepted = best_d <= max_mean_diff and offset > 0
    return StitchMatch(offset if accepted else hp, best_d, ambiguous, accepted)


def stitch_vertical(slices: list[Image.Image], template_rows: int = 48, max_mean_diff: float = 8.0,
                    min_template_std: float = 4.0) -> tuple[Image.Image, list[StitchMatch]]:
    """Stitch overlapping viewport slices captured while scrolling down."""
    if not slices:
        raise ValueError("no slices")
    canvas = slices[0].convert("RGB")
    matches: list[StitchMatch] = []
    prev = slices[0]
    for new in slices[1:]:
        m = find_overlap(prev, new, template_rows, max_mean_diff, min_template_std)
        matches.append(m)
        # Rows of `prev` before the overlap stay; `new` is appended from there.
        keep = canvas.height - prev.height + m.offset
        out = Image.new("RGB", (canvas.width, keep + new.height))
        out.paste(canvas.crop((0, 0, canvas.width, keep)), (0, 0))
        out.paste(new.convert("RGB"), (0, keep))
        canvas, prev = out, new
    return canvas, matches


# --------------------------------------------------------------------------- layout check

def page_bands(img: Image.Image, tolerance: int = 14, min_gap: int = 6,
               min_band_fraction: float = 0.05) -> Optional[list[tuple[int, int]]]:
    """Vertical extents of page content within the page's columns, split by
    rows that are canvas-coloured across those columns (the gap between two
    pages in a continuous layout). None if the page cannot be located."""
    box, canvas, mode = find_page_box(img, tolerance)
    if box is None:
        return None
    c0, _, c1, _ = box
    rgb = np.asarray(img.convert("RGB"), dtype=np.int16)[:, c0:c1]
    h = rgb.shape[0]
    tol = tolerance if mode == "bright" else (canvas_tolerance(canvas, tolerance) or tolerance)
    canvas_like = (np.abs(rgb - np.array(canvas, dtype=np.int16)).max(axis=2) <= tol).mean(axis=1) >= 0.9
    bands: list[list[int]] = []
    start = None
    for y, gap in enumerate(canvas_like):
        if not gap and start is None:
            start = y
        elif gap and start is not None:
            bands.append([start, y])
            start = None
    if start is not None:
        bands.append([start, h])
    merged: list[list[int]] = []
    for b in bands:
        if merged and b[0] - merged[-1][1] < min_gap:
            merged[-1][1] = b[1]
        else:
            merged.append(b)
    return [(a, b) for a, b in merged if (b - a) >= min_band_fraction * h]


def layout_issues(img: Image.Image, tolerance: int = 14) -> list[str]:
    """Fit-page sanity check: more than one page visible (continuous/two-up layout).

    A page touching the top/bottom edge is *not* treated as a problem: after the
    fit-page key it is how a page that exactly fills the view looks, and it
    cannot be told apart from a cut-off page by the image alone. It is reported
    in :func:`layout_diagnostics` only.
    """
    from .models import Flag

    bands = page_bands(img, tolerance)
    if bands and len(bands) > 1:
        return [Flag.MULTI_PAGE_VIEW]
    return []


def layout_diagnostics(img: Image.Image, tolerance: int = 14) -> dict:
    """Numbers behind :func:`layout_issues`, for the log."""
    box, canvas, mode = find_page_box(img, tolerance)
    w, h = img.size
    return {"viewport": [w, h], "canvas_rgb": list(canvas), "mode": mode,
            "page_box": list(box) if box else None,
            "page_touches_top_or_bottom": bool(box and (box[1] <= 1 or box[3] >= h - 1)),
            "page_bands_y": page_bands(img, tolerance)}
