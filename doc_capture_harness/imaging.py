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


def crop_to_page(img: Image.Image, tolerance: int = 14, padding: int = 4,
                 min_page_fraction: float = 0.15) -> CropResult:
    """Crop the viewport capture to the rendered page(s).

    The viewer draws pages on a uniform canvas (grey in most readers). Pixels
    that differ from the canvas colour by more than ``tolerance`` on any
    channel are treated as page content, and the bounding box of rows/columns
    that are mostly non-canvas gives the page. If the page fills the whole
    viewport, or the detection is implausibly small, the full viewport is
    returned with ``fallback=True``.
    """
    rgb = np.asarray(img.convert("RGB"), dtype=np.int16)
    h, w, _ = rgb.shape
    canvas = estimate_canvas_color(img)
    if min(canvas) >= 235:
        # The border is (near) white: the page itself fills the viewport edge,
        # so its edges cannot be located. Keep the whole viewport.
        return CropResult(img, (0, 0, w, h), True, canvas)
    diff = np.abs(rgb - np.array(canvas, dtype=np.int16)).max(axis=2) > tolerance
    # A row/column is "page" if a meaningful share of it differs from canvas;
    # this ignores thin scrollbars, page shadows and stray cursor pixels.
    row_share = diff.mean(axis=1)
    col_share = diff.mean(axis=0)
    rows = np.where(row_share > 0.15)[0]
    cols = np.where(col_share > 0.15)[0]
    if rows.size == 0 or cols.size == 0:
        return CropResult(img, (0, 0, w, h), True, canvas)
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    left, right = int(cols[0]), int(cols[-1]) + 1
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
    """Vertical extents of page content separated by canvas rows.

    Returns None when the canvas cannot be identified (near-white border).
    """
    canvas = estimate_canvas_color(img)
    if min(canvas) >= 235:
        return None
    rgb = np.asarray(img.convert("RGB"), dtype=np.int16)
    h = rgb.shape[0]
    diff = np.abs(rgb - np.array(canvas, dtype=np.int16)).max(axis=2) > tolerance
    rows = diff.mean(axis=1) > 0.15
    bands: list[list[int]] = []
    start = None
    for y, on in enumerate(rows):
        if on and start is None:
            start = y
        elif not on and start is not None:
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
    """Fit-page sanity checks on a raw viewport capture (see Flag.MULTI_PAGE_VIEW / PAGE_CUT_OFF)."""
    from .models import Flag

    bands = page_bands(img, tolerance)
    if bands is None:
        # No canvas visible on any edge: the page covers the whole viewport, so
        # parts of it are almost certainly off-screen (readers' canvases are grey
        # by default; set capture.layout_check: false for a white-canvas reader).
        return [Flag.PAGE_CUT_OFF]
    if not bands:
        return []
    issues = []
    if len(bands) > 1:
        issues.append(Flag.MULTI_PAGE_VIEW)
    h = img.height
    if bands[0][0] <= 0 or bands[-1][1] >= h:
        issues.append(Flag.PAGE_CUT_OFF)
    return issues
