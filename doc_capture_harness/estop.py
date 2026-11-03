"""Emergency stop.

Any of these stops the run at the next check point (before every keystroke,
scroll, click and capture):

* the configured global hotkey (default Ctrl+Shift+F12), polled with
  ``GetAsyncKeyState`` so no keyboard hook or extra package is needed;
* creating a file named ``STOP`` in the run directory;
* Ctrl+C in the console;
* pyautogui's fail-safe (mouse slammed into a screen corner) while a pyautogui
  fallback action is running.

The manifest is saved after every page, so a stopped run can be resumed.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .errors import EmergencyStop

_VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "esc": 0x1B,
    "pause": 0x13, "scrolllock": 0x91, "space": 0x20,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
}


def parse_hotkey(spec: str) -> list[int]:
    codes = []
    for part in spec.lower().replace(" ", "").split("+"):
        key = part.upper() if len(part) == 1 and part.isalpha() else part
        if key not in _VK:
            raise ValueError(f"unsupported hotkey part {part!r} in {spec!r}")
        codes.append(_VK[key])
    return codes


class EmergencyStopMonitor:
    def __init__(self, hotkey: Optional[str], stop_file: Optional[Path], poll_s: float = 0.05):
        self._event = threading.Event()
        self.reason: str = ""
        self.stop_file = stop_file
        self._codes = parse_hotkey(hotkey) if hotkey else []
        self._poll_s = poll_s
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self.stop_file and self.stop_file.exists():
            # A stale STOP file from a previous run must not block a resume.
            self.stop_file.unlink()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="estop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def trigger(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    @property
    def triggered(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise :class:`EmergencyStop` if a stop was requested."""
        if self.stop_file is not None and self.stop_file.exists():
            self.trigger(f"stop file present: {self.stop_file}")
        if self._event.is_set():
            raise EmergencyStop(self.reason or "emergency stop")

    def sleep(self, seconds: float) -> None:
        """Interruptible sleep."""
        end = time.monotonic() + seconds
        while True:
            self.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self._event.wait(min(remaining, 0.05))

    # ------------------------------------------------------------------ polling
    def _loop(self) -> None:
        get_state = None
        if sys.platform == "win32" and self._codes:
            import ctypes
            get_state = ctypes.windll.user32.GetAsyncKeyState
        while self._running and not self._event.is_set():
            if get_state is not None:
                if all(get_state(c) & 0x8000 for c in self._codes):
                    self.trigger("emergency-stop hotkey pressed")
                    return
            if self.stop_file is not None and self.stop_file.exists():
                self.trigger(f"stop file present: {self.stop_file}")
                return
            time.sleep(self._poll_s)
