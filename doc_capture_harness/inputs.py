"""Keyboard / mouse input with emergency-stop checks and a pyautogui fallback.

pywinauto's ``send_keys`` and ``mouse`` (SendInput) are the primary path.
pyautogui is used only if pywinauto raises and the fallback is allowed. Every
call checks the emergency stop first.
"""
from __future__ import annotations

import re
from typing import Optional

from .estop import EmergencyStopMonitor
from .logging_utils import event, get_logger

# pywinauto {KEY} names -> pyautogui key names
_KEYMAP = {
    "RIGHT": "right", "LEFT": "left", "UP": "up", "DOWN": "down",
    "PGDN": "pagedown", "PGUP": "pageup", "HOME": "home", "END": "end",
    "ENTER": "enter", "TAB": "tab", "ESC": "esc", "SPACE": "space",
    "BACKSPACE": "backspace", "BKSP": "backspace", "DELETE": "delete", "DEL": "delete",
    **{f"F{i}": f"f{i}" for i in range(1, 25)},
}
_MODS = {"^": "ctrl", "+": "shift", "%": "alt"}
_TOKEN = re.compile(r"([\^+%]*)(\{[^}]+\}|.)", re.S)


def pywinauto_to_pyautogui(keys: str) -> list[tuple[list[str], str, int]]:
    """Translate the common subset of pywinauto syntax to (modifiers, key, repeat)."""
    out = []
    for mods, tok in _TOKEN.findall(keys):
        modlist = [_MODS[m] for m in mods]
        repeat = 1
        if tok.startswith("{"):
            body = tok[1:-1].strip()
            parts = body.split()
            name = parts[0]
            if len(parts) > 1 and parts[1].isdigit():
                repeat = int(parts[1])
            key = _KEYMAP.get(name.upper(), name.lower())
        else:
            key = tok.lower() if modlist else tok
        out.append((modlist, key, repeat))
    return out


_VK = {"right": 0x27, "left": 0x25, "up": 0x26, "down": 0x28, "pagedown": 0x22, "pageup": 0x21,
       "home": 0x24, "end": 0x23, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "space": 0x20,
       "backspace": 0x08, "delete": 0x2E, "ctrl": 0x11, "shift": 0x10, "alt": 0x12,
       **{f"f{i}": 0x6F + i for i in range(1, 25)}}
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E}


def key_events(keys: str) -> list[tuple[bool, int]]:
    """pywinauto key syntax -> ordered (is_down, virtual-key) events for PostMessage."""
    out = []
    for mods, key, repeat in pywinauto_to_pyautogui(keys):
        vk = _VK.get(key) or (ord(key.upper()) if len(key) == 1 and key.isalnum() else None)
        if vk is None:
            raise ValueError(f"key {key!r} cannot be posted in background mode")
        mvks = [_VK[m] for m in mods]
        for _ in range(repeat):
            out += [(True, m) for m in mvks] + [(True, vk), (False, vk)] + [(False, m) for m in reversed(mvks)]
    return out


class InputDriver:
    def __init__(self, estop: EmergencyStopMonitor, allow_fallback: bool = True, failsafe: bool = True):
        self.estop = estop
        self.allow_fallback = allow_fallback
        self.failsafe = failsafe
        self.log = get_logger()
        self.fallback_uses = 0

    def send_keys(self, keys: str) -> None:
        self.estop.check()
        try:
            from pywinauto import keyboard
            keyboard.send_keys(keys, pause=0.03, with_spaces=True, vk_packet=False)
            return
        except Exception as exc:
            if not self.allow_fallback:
                raise
            event(self.log, "input_fallback", f"pywinauto send_keys failed ({exc}); using pyautogui",
                  keys=keys)
        self._pyautogui_keys(keys)

    def _pyautogui_keys(self, keys: str) -> None:
        import pyautogui
        pyautogui.FAILSAFE = self.failsafe
        self.fallback_uses += 1
        try:
            for mods, key, repeat in pywinauto_to_pyautogui(keys):
                for _ in range(repeat):
                    self.estop.check()
                    if mods:
                        pyautogui.hotkey(*mods, key)
                    elif len(key) == 1:
                        pyautogui.write(key)
                    else:
                        pyautogui.press(key)
        except pyautogui.FailSafeException:
            self.estop.trigger("pyautogui fail-safe (mouse moved to a screen corner)")
            self.estop.check()

    def post_keys(self, hwnd: int, keys: str) -> None:
        """Post WM_KEYDOWN/WM_KEYUP to ``hwnd``: no focus change, no global input.
        shortcut: modifiers are posted as messages only, so readers that check
        GetKeyState (e.g. for Ctrl+0) see no Ctrl; use plain keys in background mode."""
        from . import platform_win as pw
        import ctypes
        for down, vk in key_events(keys):
            self.estop.check()
            scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
            lp = 1 | (scan << 16) | ((1 << 24) if vk in _EXTENDED else 0) | (0 if down else 0xC0000000)
            pw.post_message(hwnd, 0x0100 if down else 0x0101, vk, lp)
            self.estop.sleep(0.02)

    def post_wheel(self, hwnd: int, x: int, y: int, wheel_dist: int) -> None:
        """WM_MOUSEWHEEL to ``hwnd`` at screen point (x, y), without moving the cursor."""
        from . import platform_win as pw
        self.estop.check()
        wp = ((int(wheel_dist) * 120) & 0xFFFF) << 16
        lp = ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)
        pw.post_message(hwnd, 0x020A, wp, lp)

    def scroll(self, x: int, y: int, wheel_dist: int) -> None:
        self.estop.check()
        try:
            from pywinauto import mouse
            mouse.scroll(coords=(int(x), int(y)), wheel_dist=int(wheel_dist))
            return
        except Exception as exc:
            if not self.allow_fallback:
                raise
            event(self.log, "input_fallback", f"pywinauto scroll failed ({exc}); using pyautogui")
        import pyautogui
        pyautogui.FAILSAFE = self.failsafe
        self.fallback_uses += 1
        try:
            pyautogui.scroll(int(wheel_dist) * 120, x=int(x), y=int(y))
        except pyautogui.FailSafeException:
            self.estop.trigger("pyautogui fail-safe (mouse moved to a screen corner)")
            self.estop.check()

    def click(self, x: int, y: int) -> None:
        self.estop.check()
        try:
            from pywinauto import mouse
            mouse.click(button="left", coords=(int(x), int(y)))
            return
        except Exception as exc:
            if not self.allow_fallback:
                raise
            event(self.log, "input_fallback", f"pywinauto click failed ({exc}); using pyautogui")
        import pyautogui
        pyautogui.FAILSAFE = self.failsafe
        self.fallback_uses += 1
        pyautogui.click(int(x), int(y))

    def move_cursor(self, x: int, y: int) -> None:
        self.estop.check()
        try:
            from . import platform_win
            platform_win.set_cursor_pos(x, y)
        except Exception:
            import pyautogui
            pyautogui.FAILSAFE = self.failsafe
            pyautogui.moveTo(int(x), int(y))


def describe_keys(keys: Optional[str]) -> str:
    return keys or "<none>"
