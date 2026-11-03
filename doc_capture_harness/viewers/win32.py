"""Generic Win32 / UI Automation viewer adapter (pywinauto + ctypes).

Everything reader-specific comes from :class:`ViewerConfig`, so this class is
the ``generic`` profile as-is; Acrobat and PDF-XChange subclass it only for
small overrides.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from .. import platform_win as pw
from ..config import ViewerConfig
from ..errors import FocusLost
from ..estop import EmergencyStopMonitor
from ..inputs import InputDriver
from ..logging_utils import event, get_logger
from ..models import DialogInfo, PageIndicator, Rect
from .base import CheckpointFn, ViewerAdapter

IGNORED_FOREIGN_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "tooltips_class32",
    "NotifyIconOverflowWindow", "Windows.UI.Core.CoreWindow", "ForegroundStaging",
}
IGNORED_VIEWER_CLASSES = {"tooltips_class32", "IME", "MSCTFIME UI"}


class Win32ViewerAdapter(ViewerAdapter):
    name = "generic"

    def __init__(self, cfg: ViewerConfig, estop: EmergencyStopMonitor, checkpoint: CheckpointFn,
                 inputs: InputDriver, ignore_title_re: Optional[list[str]] = None):
        super().__init__(cfg, estop, checkpoint)
        self.inputs = inputs
        self.log = get_logger()
        self.hwnd: int = 0
        self.pid: int = 0
        self.launched_pid: Optional[int] = None
        self._pane_hwnd: Optional[int] = None
        self._pane_warned = False
        self._uia = None
        self._indicator_controls: Optional[list] = None
        self._ignore_title = [re.compile(p) for p in (ignore_title_re or [])]

    # ------------------------------------------------------------------ lifecycle
    def _executable(self) -> str:
        if self.cfg.executable:
            if not Path(self.cfg.executable).exists():
                raise FileNotFoundError(f"viewer.executable not found: {self.cfg.executable}")
            return self.cfg.executable
        for c in self.cfg.executable_candidates:
            if Path(os.path.expandvars(c)).exists():
                return os.path.expandvars(c)
        raise FileNotFoundError(
            "No reader executable found. Set viewer.executable in the config. Tried: "
            + ", ".join(self.cfg.executable_candidates))

    def launch(self, document: Path) -> None:
        exe = self._executable()
        args = [a.replace("{document}", str(document)) for a in (self.cfg.launch_args or ["{document}"])]
        event(self.log, "viewer_launch", f"launching {exe}", exe=exe, args=args)
        proc = subprocess.Popen([exe, *args], close_fds=True)  # no shell
        self.launched_pid = proc.pid

    def _title_re(self, document: Path) -> Optional[re.Pattern]:
        if not self.cfg.window_title_re:
            return None
        pat = (self.cfg.window_title_re
               .replace("{stem}", re.escape(document.stem))
               .replace("{name}", re.escape(document.name)))
        return re.compile(pat)

    def _candidates(self, document: Path) -> list[pw.TopWindow]:
        title_re = self._title_re(document)
        class_re = re.compile(self.cfg.window_class_re) if self.cfg.window_class_re else None
        names = {n.lower() for n in self.cfg.process_names}
        out = []
        for w in pw.top_level_windows():
            if w.owner:          # owned windows are dialogs, not main windows
                continue
            if class_re and not class_re.search(w.class_name):
                continue
            if title_re and not title_re.search(w.title):
                continue
            if names and os.path.basename(pw.process_image_name(w.pid)).lower() not in names:
                continue
            out.append(w)
        return out

    def attach(self, document: Path, timeout_s: float) -> None:
        while True:
            deadline = time.monotonic() + timeout_s
            cands: list[pw.TopWindow] = []
            while time.monotonic() < deadline:
                self.estop.check()
                cands = self._candidates(document)
                if cands:
                    # Give a single-instance hand-off a moment to settle.
                    self.estop.sleep(0.5)
                    cands = self._candidates(document) or cands
                    break
                self.estop.sleep(0.5)
            if len(cands) == 1:
                return self._bind(cands[0])
            if len(cands) > 1:
                same = [c for c in cands if c.pid == self.launched_pid]
                if len(same) == 1:
                    return self._bind(same[0])
                titles = "\n".join(f"  - {c.title!r} (pid {c.pid})" for c in cands)
                self.checkpoint(
                    "ambiguous_window",
                    f"Several reader windows match:\n{titles}\n\nClick on the window that shows "
                    f"'{document.name}' so it is in front, then choose Retry.")
                fg = pw.foreground_window()
                match = [c for c in cands if c.hwnd == fg or pw.root_owner(fg) == c.hwnd]
                if match:
                    return self._bind(match[0])
                continue
            self.checkpoint(
                "window_not_found",
                f"No reader window matching the configuration appeared within {timeout_s:.0f}s.\n"
                f"Open '{document}' in the reader manually (or fix viewer.window_title_re / "
                f"process_names), then choose Retry.")

    def _bind(self, w: pw.TopWindow) -> None:
        from pywinauto import Desktop

        self.hwnd, self.pid = w.hwnd, w.pid
        self._pane_hwnd = None
        self._indicator_controls = None
        self._uia = Desktop(backend="uia").window(handle=w.hwnd)
        event(self.log, "viewer_attached", f"attached to {w.title!r}", hwnd=w.hwnd, pid=w.pid,
              class_name=w.class_name, exe=pw.process_image_name(w.pid))
        if pw.is_minimized(self.hwnd):
            pw.restore_window(self.hwnd)

    def is_alive(self) -> bool:
        return bool(self.hwnd) and pw.is_window(self.hwnd)

    def close(self) -> None:
        if self.is_alive():
            try:
                self._uia.wrapper_object().close()
            except Exception as exc:
                event(self.log, "viewer_close_failed", str(exc))

    # ------------------------------------------------------------------ geometry
    def main_handle(self) -> int:
        return self.hwnd

    def window_rect(self) -> Rect:
        return pw.get_window_rect(self.hwnd)

    def _find_pane(self) -> Optional[int]:
        sel = self.cfg.document_pane
        if not any([sel.class_name, sel.class_name_re, sel.title, sel.title_re]):
            return None
        cre = re.compile(sel.class_name_re) if sel.class_name_re else None
        tre = re.compile(sel.title_re) if sel.title_re else None
        hits = []
        for h, cls, title, rect in pw.child_windows(self.hwnd):
            if sel.class_name and cls != sel.class_name:
                continue
            if cre and not cre.search(cls):
                continue
            if sel.title is not None and title != sel.title:
                continue
            if tre and not tre.search(title):
                continue
            if rect.area > 0:
                hits.append((h, rect))
        if not hits:
            return None
        if sel.pick == "first":
            return hits[0][0]
        return max(hits, key=lambda x: x[1].area)[0]

    def document_rect(self) -> Rect:
        if self._pane_hwnd and pw.is_window(self._pane_hwnd) and pw.is_visible(self._pane_hwnd):
            r = pw.get_window_rect(self._pane_hwnd)
            if r.area > 0:
                return r
        self._pane_hwnd = self._find_pane()
        if self._pane_hwnd:
            return pw.get_window_rect(self._pane_hwnd)
        if not self._pane_warned:
            event(self.log, "document_pane_not_found",
                  "document pane selector not configured/matched; using client area minus "
                  "viewport_margins (run `calibrate` to configure)", level=30)
            self._pane_warned = True
        m = self.cfg.viewport_margins
        return pw.get_client_rect_screen(self.hwnd).inset(*m)

    def is_foreground(self) -> bool:
        fg = pw.foreground_window()
        return fg == self.hwnd or pw.root_owner(fg) == self.hwnd

    def bring_to_foreground(self) -> bool:
        self.estop.check()
        if pw.is_minimized(self.hwnd):
            pw.restore_window(self.hwnd)
        try:
            self._uia.wrapper_object().set_focus()
        except Exception as exc:
            event(self.log, "set_focus_failed", str(exc), level=30)
        self.estop.sleep(0.15)
        return self.is_foreground()

    def is_busy(self) -> Optional[bool]:
        b = self.cfg.busy
        if b.check_hung and pw.is_hung(self.hwnd):
            return True
        if b.title_re and re.search(b.title_re, pw.get_window_text(self.hwnd) or ""):
            return True
        if b.check_cursor and pw.cursor_is_busy():
            return True
        return False

    def capture_protected(self) -> bool:
        handles = [self.hwnd] + ([self._pane_hwnd] if self._pane_hwnd else [])
        return any(pw.display_affinity(h) != 0 for h in handles)

    def environment_info(self) -> dict:
        wr = self.window_rect()
        dr = self.document_rect()
        mons = pw.monitors()
        doc_mons = [m for m in mons if m.rect.intersects(dr)]
        return {
            "os": pw.os_version(),
            "dpi_awareness": pw.current_dpi_awareness(),
            "viewer_exe": pw.process_image_name(self.pid),
            "viewer_pid": self.pid,
            "window_title": pw.get_window_text(self.hwnd),
            "window_rect": wr.to_dict(),
            "document_rect": dr.to_dict(),
            "document_pane_found": bool(self._pane_hwnd),
            "window_dpi": pw.window_dpi(self.hwnd),
            "monitors": [m.to_dict() for m in mons],
            "document_monitors": [m.device for m in doc_mons],
            "virtual_screen": pw.virtual_screen().to_dict(),
        }

    def screen_layout(self, rect: Rect):
        return pw.virtual_screen(), pw.monitors_for_rect(rect)

    def park_cursor(self) -> None:
        wr = self.window_rect()
        dr = self.document_rect()
        # Title bar of the viewer window: inside the viewer (so a busy cursor is
        # still reported) but outside the document viewport.
        x, y = wr.left + max(40, wr.width // 3), wr.top + 6
        if dr.top <= y < dr.bottom:
            x, y = dr.left + 2, max(wr.top + 2, dr.top - 6)
        self.inputs.move_cursor(x, y)

    def grab_window(self) -> Optional[Image.Image]:
        import win32gui
        import win32ui

        r = self.window_rect()
        w, h = r.width, r.height
        hwnd_dc = win32gui.GetWindowDC(self.hwnd)
        mfc = win32ui.CreateDCFromHandle(hwnd_dc)
        save = mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        try:
            bmp.CreateCompatibleBitmap(mfc, w, h)
            save.SelectObject(bmp)
            ok = pw.print_window(self.hwnd, save.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
            if not ok:
                return None
            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
        finally:
            win32gui.DeleteObject(bmp.GetHandle())
            save.DeleteDC()
            mfc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hwnd_dc)

    # ------------------------------------------------------------------ page indicator
    def _control_text(self, ctrl) -> str:
        parts = []
        try:
            parts.append(ctrl.element_info.name or "")
        except Exception:
            pass
        try:
            v = ctrl.iface_value.CurrentValue
            if v:
                parts.append(str(v))
        except Exception:
            pass
        return " ".join(p for p in parts if p).strip()

    def _scan_indicator_controls(self) -> list:
        icfg = self.cfg.page_indicator
        name_re = re.compile(icfg.control_name_re) if icfg.control_name_re else None
        found = []
        wrapper = self._uia.wrapper_object()
        for ct in icfg.control_types:
            try:
                ctrls = wrapper.descendants(control_type=ct)
            except Exception:
                continue
            for c in ctrls[: icfg.max_uia_controls]:
                text = self._control_text(c)
                if not text or (name_re and not name_re.search(text)):
                    continue
                if any(re.search(p, text) for p in icfg.patterns + icfg.total_patterns):
                    found.append(c)
        return found

    def _parse_indicator(self, texts: list[tuple[str, str]], source: str) -> PageIndicator:
        icfg = self.cfg.page_indicator
        current = total = None
        raw = []
        for ctype, text in texts:
            for p in icfg.patterns:
                m = re.search(p, text)
                if not m:
                    continue
                gd = m.groupdict()
                if gd.get("total") and current is None:
                    current, total = int(gd["current"]), int(gd["total"])
                    raw.append(text)
                elif gd.get("current") and current is None and ctype in ("Edit", "ComboBox", "ocr"):
                    current = int(gd["current"])
                    raw.append(text)
                break
            if total is None:
                for p in icfg.total_patterns:
                    m = re.search(p, text)
                    if m and m.groupdict().get("total"):
                        total = int(m.group("total"))
                        raw.append(text)
                        break
        if current is not None and total is not None and not (1 <= current <= total):
            return PageIndicator(None, None, " | ".join(raw), source + ":implausible")
        return PageIndicator(current, total, " | ".join(raw), source if current is not None else "none")

    def read_page_indicator(self) -> PageIndicator:
        icfg = self.cfg.page_indicator
        if not icfg.enabled:
            return PageIndicator()
        try:
            if self._indicator_controls is None:
                self._indicator_controls = self._scan_indicator_controls()
            texts = [(c.element_info.control_type, self._control_text(c)) for c in self._indicator_controls]
            ind = self._parse_indicator(texts, "uia")
            if ind.readable:
                return ind
            self._indicator_controls = None   # stale cache: rescan next time
        except Exception as exc:
            self._indicator_controls = None
            event(self.log, "indicator_uia_error", str(exc), level=10)
        if icfg.ocr_region:
            try:
                from ..ocr import quick_text
                from ..config import OcrConfig
                import mss
                x, y, w, h = icfg.ocr_region
                wr = self.window_rect()
                with mss.mss() as s:
                    shot = s.grab({"left": wr.left + x, "top": wr.top + y, "width": w, "height": h})
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
                text = quick_text(img, OcrConfig()).strip()
                return self._parse_indicator([("ocr", text)], "ocr")
            except Exception as exc:
                event(self.log, "indicator_ocr_error", str(exc), level=10)
        return PageIndicator()

    # ------------------------------------------------------------------ dialogs
    def _describe(self, w: pw.TopWindow, owned: bool) -> DialogInfo:
        texts, buttons = [], []
        for h, cls, text, _r in pw.child_windows(w.hwnd):
            if not text:
                continue
            if cls == "Button":
                buttons.append(text.replace("&", ""))
            else:
                texts.append(text)
        if not texts and not buttons:
            try:
                from pywinauto import Desktop
                wrapper = Desktop(backend="uia").window(handle=w.hwnd).wrapper_object()
                for c in wrapper.descendants()[:80]:
                    name = (c.element_info.name or "").strip()
                    if not name:
                        continue
                    (buttons if c.element_info.control_type == "Button" else texts).append(name)
            except Exception:
                pass
        return DialogInfo(w.hwnd, w.title, w.class_name, w.pid, w.rect, texts[:40], buttons[:20], owned)

    def list_dialogs(self, doc_rect: Optional[Rect]) -> list[DialogInfo]:
        out: list[DialogInfo] = []
        own = {pw.own_pid()}
        console = pw.console_window()
        above_main = True
        for w in pw.top_level_windows():
            if w.hwnd == self.hwnd:
                above_main = False
                continue
            if w.pid in own or w.hwnd == console or w.rect.area == 0:
                continue
            if any(p.search(w.title) for p in self._ignore_title):
                continue
            if w.pid == self.pid:
                if w.class_name in IGNORED_VIEWER_CLASSES or w.rect.width < 40 or w.rect.height < 20:
                    continue
                is_dialog = (w.class_name == "#32770" or w.owner == self.hwnd
                             or pw.root_owner(w.hwnd) == self.hwnd
                             or (bool(w.title) and not (w.exstyle & pw.WS_EX_TOOLWINDOW)))
                if is_dialog:
                    out.append(self._describe(w, True))
            elif above_main and doc_rect is not None and w.rect.intersects(doc_rect):
                if w.class_name in IGNORED_FOREIGN_CLASSES or (w.exstyle & pw.WS_EX_TRANSPARENT):
                    continue
                out.append(self._describe(w, False))
        return out

    def click_dialog_button(self, dialog: DialogInfo, button: str) -> bool:
        self.estop.check()
        want = button.replace("&", "").strip().lower()
        try:
            from pywinauto import Desktop
            dlg = Desktop(backend="uia").window(handle=dialog.handle)
            for b in dlg.wrapper_object().descendants(control_type="Button"):
                if (b.element_info.name or "").replace("&", "").strip().lower() == want:
                    b.invoke()
                    return True
        except Exception as exc:
            event(self.log, "dialog_uia_click_failed", str(exc), level=10)
        # Win32 fallback: BM_CLICK on a matching child button.
        for h, cls, text, _r in pw.child_windows(dialog.handle):
            if cls == "Button" and text.replace("&", "").strip().lower() == want:
                pw.post_message(h, 0x00F5)  # BM_CLICK
                return True
        return False

    def send_dialog_keys(self, dialog: DialogInfo, keys: str) -> bool:
        self.estop.check()
        try:
            from pywinauto import Desktop
            Desktop(backend="uia").window(handle=dialog.handle).wrapper_object().set_focus()
        except Exception:
            return False
        self.estop.sleep(0.1)
        if pw.foreground_window() != dialog.handle:
            return False
        self.inputs.send_keys(keys)
        return True

    # ------------------------------------------------------------------ input
    def _ensure_focus(self) -> None:
        for _ in range(3):
            if self.is_foreground():
                return
            self.bring_to_foreground()
        if not self.is_foreground():
            raise FocusLost("viewer window could not be brought to the foreground")

    def focus_document(self) -> None:
        self._ensure_focus()
        if self._pane_hwnd:
            try:
                from pywinauto import Desktop
                Desktop(backend="uia").window(handle=self._pane_hwnd).wrapper_object().set_focus()
                return
            except Exception:
                pass
        if self.cfg.focus_pane_by_click:
            dr = self.document_rect()
            # Left edge of the viewport is canvas in fit-page view (no links/forms).
            self.inputs.click(dr.left + 6, dr.center[1])

    def send_keys(self, keys: str) -> None:
        self._ensure_focus()
        self.inputs.send_keys(keys)

    def wheel(self, notches: int) -> None:
        self._ensure_focus()
        x, y = self.document_rect().center
        self.inputs.scroll(x, y, notches)
        self.park_cursor()

    def open_goto_dialog_and_enter(self, n: int) -> bool:
        if not self.cfg.keys.goto_page or not self.cfg.goto_dialog_title_re:
            return False
        title_re = re.compile(self.cfg.goto_dialog_title_re)
        self.send_keys(self.cfg.keys.goto_page)
        deadline = time.monotonic() + 5
        dlg = None
        while time.monotonic() < deadline and dlg is None:
            self.estop.sleep(0.15)
            dlg = next((d for d in self.list_dialogs(None) if d.owned_by_viewer and title_re.search(d.title)), None)
        if dlg is None:
            return False
        if not self.send_dialog_keys(dlg, "^a" + str(n) + "{ENTER}"):
            return False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.estop.sleep(0.15)
            if not pw.is_window(dlg.handle) or not pw.is_visible(dlg.handle):
                return True
        return False

    def enter_page_number(self, n: int) -> bool:
        if self._indicator_controls is None:
            self.read_page_indicator()
        edits = [c for c in (self._indicator_controls or []) if c.element_info.control_type == "Edit"]
        if not edits:
            return False
        try:
            edit = edits[0]
            self._ensure_focus()
            edit.set_focus()
            edit.set_edit_text(str(n))
            self.inputs.send_keys("{ENTER}")
            self.estop.sleep(0.2)
            self.focus_document()
            return True
        except Exception as exc:
            event(self.log, "page_field_failed", str(exc), level=30)
            return False
