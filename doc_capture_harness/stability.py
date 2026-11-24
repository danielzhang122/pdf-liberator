"""Wait for the rendered document to stop changing.

Rather than sleeping a fixed time after each action, the harness captures the
viewport repeatedly and returns once ``required_stable_frames`` consecutive
frames are (near-)identical and the viewer does not report itself busy. A
small ``min_wait_s`` covers the gap between a keystroke and the first repaint.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from PIL import Image

from .config import StabilityConfig
from .imaging import changed_fraction


def _crop(img: Image.Image, region: Optional[tuple[int, int, int, int]]) -> Image.Image:
    if region is None:
        return img
    l, t, r, b = region
    l, t = max(0, l), max(0, t)
    r, b = min(img.width, r), min(img.height, b)
    return img.crop((l, t, r, b)) if r - l >= 32 and b - t >= 32 else img


@dataclass
class StabilityResult:
    stable: bool
    frame: Optional[Image.Image]
    frames: int
    elapsed_s: float
    busy_seen: bool
    last_change: float
    history: list[float] = field(default_factory=list)


def wait_for_stable(grab: Callable[[], Image.Image], cfg: StabilityConfig,
                    sleep: Callable[[float], None] = time.sleep,
                    is_busy: Optional[Callable[[], Optional[bool]]] = None,
                    timeout_s: Optional[float] = None,
                    region: Optional[tuple[int, int, int, int]] = None) -> StabilityResult:
    """``region`` (left, top, right, bottom) limits the comparison to the page
    area, so UI animation elsewhere in the view (toolbars, spinners, carets)
    cannot keep the page from being considered stable."""
    timeout = cfg.timeout_s if timeout_s is None else timeout_s

    def part(img: Image.Image) -> Image.Image:
        return _crop(img, region)

    start = time.monotonic()
    sleep(cfg.min_wait_s)
    prev = grab()
    frames, run, busy_seen = 1, 1, False
    history: list[float] = []
    last = 1.0
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return StabilityResult(False, prev, frames, elapsed, busy_seen, last, history)
        # Poll quickly while the view is still changing; slow down once frames agree, so the
        # confirmation frames stay spread out (the reliability part of the check).
        sleep(cfg.fast_interval_s if last > cfg.changed_fraction_threshold else cfg.interval_s)
        cur = grab()
        frames += 1
        last = changed_fraction(part(prev), part(cur), cfg.pixel_threshold, cfg.downscale)
        history.append(round(last, 5))
        busy = bool(is_busy()) if is_busy else False
        busy_seen = busy_seen or busy
        if last <= cfg.changed_fraction_threshold and not busy:
            run += 1
        else:
            run = 1
        prev = cur
        if run >= cfg.required_stable_frames:
            return StabilityResult(True, cur, frames, time.monotonic() - start, busy_seen, last, history)


def confirm_stable(prev: Image.Image, grab: Callable[[], Image.Image], cfg: StabilityConfig,
                   sleep: Callable[[float], None] = time.sleep,
                   is_busy: Optional[Callable[[], Optional[bool]]] = None,
                   region: Optional[tuple[int, int, int, int]] = None) -> Optional[Image.Image]:
    """Cheap check for a view that was already seen stable: one fresh frame, a short
    pause, must match ``prev`` and the viewer must not be busy. Returns the fresh frame,
    or None when a full ``wait_for_stable`` is needed."""
    sleep(cfg.fast_interval_s)
    cur = grab()
    if is_busy is not None and is_busy():
        return None
    if changed_fraction(_crop(prev, region), _crop(cur, region), cfg.pixel_threshold, cfg.downscale) \
            <= cfg.changed_fraction_threshold:
        return cur
    return None
