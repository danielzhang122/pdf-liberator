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
                    timeout_s: Optional[float] = None) -> StabilityResult:
    timeout = cfg.timeout_s if timeout_s is None else timeout_s
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
        sleep(cfg.interval_s)
        cur = grab()
        frames += 1
        last = changed_fraction(prev, cur, cfg.pixel_threshold, cfg.downscale)
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
