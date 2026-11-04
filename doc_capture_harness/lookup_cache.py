"""Throttled caching for expensive UI lookups.

Searching a reader's UI Automation tree can take seconds (Acrobat exposes the
whole document's text through it). A lookup is therefore done once and its
result reused; when the result is empty or goes stale, the search is retried
at most every ``retry_after_s`` seconds instead of on every call.
"""
from __future__ import annotations

import time
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")


class ScanCache(Generic[T]):
    def __init__(self, scan: Callable[[], T], retry_after_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self._scan = scan
        self.retry_after_s = retry_after_s
        self._clock = clock
        self._value: Optional[T] = None
        self._last_scan: Optional[float] = None
        self.scans = 0

    def get(self) -> Optional[T]:
        """The cached value; scans if there is none and the last scan is old enough."""
        if self._value:
            return self._value
        now = self._clock()
        if self._last_scan is None or now - self._last_scan >= self.retry_after_s:
            self._last_scan = now
            self.scans += 1
            self._value = self._scan()
        return self._value

    def mark_stale(self) -> None:
        """Drop the value; the next scan still waits for ``retry_after_s``."""
        self._value = None

    def reset(self) -> None:
        """Drop the value and allow an immediate rescan (e.g. new window)."""
        self._value = None
        self._last_scan = None
