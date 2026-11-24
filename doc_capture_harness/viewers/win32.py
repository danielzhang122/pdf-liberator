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
        from ..lookup_cache import ScanCache
        self._indicator_cache = ScanCache(self._scan_indicator, retry_after_s=60.0)
        self._desc_cache: dict[int, tuple] = {}
        self._pane_el = None
        # Page labels of the document (e.g. i, ii, 1, 2 ...), if readable: Acrobat's Go To
        # Page dialog takes labels when "Use logical page numbers" is on (the default).
        self.page_labels: Optional[list[str]] = None
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
        self._indicator_cache.reset()
        self._desc_cache.clear()
        self._pane_el = None
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
        if self.cfg.background:
            return True                      # background mode never needs (or takes) the focus
        fg = pw.foreground_window()
        return fg == self.hwnd or pw.root_owner(fg) == self.hwnd

    def bring_to_foreground(self) -> bool:
        self.estop.check()
        if self.cfg.background:
            return True
        if pw.is_minimized(self.hwnd):
            pw.restore_window(self.hwnd)
        try:
            pw.force_foreground(self.hwnd)          # works while another process holds the foreground
        except Exception as exc:
            event(self.log, "force_foreground_failed", str(exc), level=10)
        self.estop.sleep(0.1)
        if self.is_foreground():
            return True
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

    def page_count_hint(self) -> Optional[int]:
        """Open the go-to-page dialog, read "of N" from it, and cancel it (3 attempts)."""
        if not self.cfg.keys.goto_page or not self.cfg.goto_dialog_title_re:
            return None
        dlg = None
        for attempt in range(3):
            self.close_stray_goto_dialogs()
            try:
                self.send_keys(self.cfg.keys.goto_page)
            except FocusLost as exc:
                event(self.log, "page_count_probe", f"attempt {attempt + 1}: {exc}", level=30)
                continue
            dlg = self._wait_goto_dialog()
            if dlg is not None:
                break
            event(self.log, "page_count_probe", f"attempt {attempt + 1}: go-to-page dialog did not appear",
                  level=30)
        if dlg is None:
            return None
        total = None
        for t in dlg.texts + dlg.buttons:
            m = re.search(r"(?i)\bof\s+(\d+)", t) or re.search(r"/\s*(\d+)\s*\)?\s*$", t)
            if m:
                total = int(m.group(1))
                break
        event(self.log, "page_count_probe", f"go-to-page dialog: total pages = {total}", level=20,
              texts=dlg.texts, buttons=dlg.buttons)
        self._close_dialog(dlg)
        return total if total and total > 0 else None

    def focus_info(self) -> dict:
        info: dict = {}
        try:
            fg = pw.foreground_window()
            info.update(fg_hwnd=fg, fg_is_viewer=(fg == self.hwnd), fg_class=pw.get_class_name(fg) if fg else "",
                        fg_title=pw.get_window_text(fg) if fg else "", fg_pid=pw.window_pid(fg) if fg else 0)
        except Exception as exc:
            info["fg_error"] = str(exc)
        try:
            # Plain Win32 (no UI Automation call into the page view, which can make
            # Acrobat start its slow accessibility pass).
            fh = pw.focused_window()
            info.update(focus_hwnd=fh, focus_class=pw.get_class_name(fh) if fh else "",
                        focus_is_pane=bool(self._pane_hwnd) and fh == self._pane_hwnd)
        except Exception as exc:
            info["focus_error"] = str(exc)
        return info

    def find_overlays(self, doc_rect: Optional[Rect]) -> list[str]:
        """Exact-name UI Automation search (native FindFirst, fast) for each name in
        ``viewer.overlay_names``; reports the ones shown over ``doc_rect``.
        Best effort: any failure means "none found"."""
        if not self.cfg.overlay_names or doc_rect is None:
            return []
        found: list[str] = []
        try:
            from pywinauto.uia_defines import IUIA

            uia = IUIA()
            root = self._uia.wrapper_object().element_info.element
            for name in self.cfg.overlay_names:
                cond = uia.iuia.CreatePropertyCondition(uia.UIA_dll.UIA_NamePropertyId, name)
                el = root.FindFirst(uia.tree_scope["descendants"], cond)
                if not el:
                    continue
                r = el.CurrentBoundingRectangle
                rect = Rect(int(r.left), int(r.top), int(r.right), int(r.bottom))
                if not el.CurrentIsOffscreen and not rect.is_empty() and rect.intersects(doc_rect):
                    found.append(name)
        except Exception as exc:  # noqa: BLE001 - detection is optional
            event(self.log, "overlay_scan_failed", f"{type(exc).__name__}: {exc}", level=10)
        return found

    def park_cursor(self) -> None:
        if self.cfg.background:
            return
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

    # ------------------------------------------------------------------ native UI Automation
    # pywinauto's descendants() builds a Python wrapper for every element of the
    # whole tree. Acrobat exposes the document's text through that tree, so a
    # full walk takes seconds (and can trigger Acrobat's slow accessibility
    # "reading" pass). These helpers walk natively, never enter the document
    # pane, and are bounded in node count and time.
    _CT_BUTTON, _CT_COMBOBOX, _CT_EDIT, _CT_TEXT = 50000, 50003, 50004, 50020
    _CT_NAMES = {50000: "Button", 50003: "ComboBox", 50004: "Edit", 50020: "Text"}

    @staticmethod
    def _iuia():
        from pywinauto.uia_defines import IUIA
        return IUIA()

    def _walk(self, root_hwnds: list[int], want, *, max_nodes: int, max_s: float,
              max_depth: int = 25) -> list:
        """Depth-first walk of the UIA control view under ``root_hwnds``,
        skipping the document pane; returns elements for which ``want(el)``."""
        u = self._iuia()
        walker = u.iuia.ControlViewWalker
        skip = {h for h in (self._pane_hwnd,) if h}
        deadline = time.monotonic() + max_s
        stack, out, visited = [], [], 0
        for h in root_hwnds:
            try:
                stack.append((u.iuia.ElementFromHandle(h), 0))
            except Exception:
                continue
        while stack and visited < max_nodes and time.monotonic() < deadline:
            el, depth = stack.pop()
            visited += 1
            try:
                if el.CurrentNativeWindowHandle in skip:
                    continue          # the page view: document content lives below it
                if want(el):
                    out.append(el)
                if depth >= max_depth:
                    continue
                kids = []
                child = walker.GetFirstChildElement(el)
                while child:
                    kids.append(child)
                    child = walker.GetNextSiblingElement(child)
                stack.extend((k, depth + 1) for k in reversed(kids))
            except Exception:
                continue
        return out

    @staticmethod
    def _el_text(el) -> str:
        parts = []
        try:
            parts.append(el.CurrentName or "")
        except Exception:
            pass
        try:
            from pywinauto.uia_defines import get_elem_interface
            v = get_elem_interface(el, "Value").CurrentValue
            if v:
                parts.append(str(v))
        except Exception:
            pass
        return " ".join(p for p in parts if p).strip()

    def _viewer_popups(self) -> list[int]:
        """The reader's own floating tool windows (newer Acrobat puts the page
        navigation box in one of them)."""
        return [w.hwnd for w in pw.top_level_windows()
                if w.pid == self.pid and w.hwnd != self.hwnd and not w.title and w.rect.area > 0]

    # ------------------------------------------------------------------ page indicator
    def _scan_indicator(self) -> list[tuple[str, object]]:
        icfg = self.cfg.page_indicator
        name_re = re.compile(icfg.control_name_re) if icfg.control_name_re else None
        pats = [re.compile(p) for p in icfg.patterns + icfg.total_patterns]
        wanted_types = {t for t, n in self._CT_NAMES.items() if n in icfg.control_types}

        def want(el) -> bool:
            if el.CurrentControlType not in wanted_types:
                return False
            text = self._el_text(el)
            if not text or (name_re and not name_re.search(text)):
                return False
            return any(p.search(text) for p in pats)

        t0 = time.monotonic()
        els = self._walk([self.hwnd] + self._viewer_popups(), want,
                         max_nodes=icfg.max_uia_controls * 10, max_s=3.0)
        found = [(self._CT_NAMES.get(el.CurrentControlType, "Text"), el) for el in els]
        event(self.log, "indicator_scan", f"page-number search: {len(found)} candidate(s) in "
              f"{time.monotonic() - t0:.1f}s", level=10)
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
        """Read the page box from cached UI elements (a few COM calls). The
        search for those elements runs once, and is retried at most once a
        minute while nothing usable is found."""
        icfg = self.cfg.page_indicator
        if not icfg.enabled:
            return PageIndicator()
        try:
            controls = self._indicator_cache.get() or []
            if controls:
                ind = self._parse_indicator([(ct, self._el_text(el)) for ct, el in controls], "uia")
                if ind.readable:
                    return ind
                self._indicator_cache.mark_stale()
        except Exception as exc:
            self._indicator_cache.mark_stale()
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
        """Title, texts and buttons of a window; cached per window while its title
        and class are unchanged (described once, not on every dialog check)."""
        key = (w.title, w.class_name)
        hit = self._desc_cache.get(w.hwnd)
        if hit and hit[0] == key:
            d = hit[1]
            return DialogInfo(w.hwnd, w.title, w.class_name, w.pid, w.rect, d.texts, d.buttons, owned)
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
                for el in self._walk([w.hwnd], lambda e: bool((e.CurrentName or "").strip()),
                                     max_nodes=200, max_s=1.0):
                    name = el.CurrentName.strip()
                    (buttons if el.CurrentControlType == self._CT_BUTTON else texts).append(name)
            except Exception:
                pass
        d = DialogInfo(w.hwnd, w.title, w.class_name, w.pid, w.rect, texts[:40], buttons[:20], owned)
        if len(self._desc_cache) > 200:
            self._desc_cache.clear()
        self._desc_cache[w.hwnd] = (key, d)
        return d

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
        if self.cfg.background:
            self.inputs.post_keys(dialog.handle, keys)
            return True
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
        for attempt in range(6):                     # about 3 s in total before giving up
            if self.is_foreground():
                return
            if self.bring_to_foreground():
                return
            self.estop.sleep(0.1 * (attempt + 1))
        if not self.is_foreground():
            raise FocusLost("viewer window could not be brought to the foreground")

    def focus_document(self) -> None:
        if self.cfg.background:
            return
        self._ensure_focus()
        if self._pane_hwnd:
            try:
                if self._pane_el is None or self._pane_el[0] != self._pane_hwnd:
                    self._pane_el = (self._pane_hwnd, self._iuia().iuia.ElementFromHandle(self._pane_hwnd))
                self._pane_el[1].SetFocus()      # one native call (was a pywinauto lookup per key)
                return
            except Exception:
                self._pane_el = None
        if self.cfg.focus_pane_by_click:
            dr = self.document_rect()
            # Left edge of the viewport is canvas in fit-page view (no links/forms).
            self.inputs.click(dr.left + 6, dr.center[1])

    def _key_target(self) -> int:
        self.document_rect()                 # refreshes _pane_hwnd
        return self._pane_hwnd or self.hwnd

    def send_keys(self, keys: str) -> None:
        if self.cfg.background:
            self.inputs.post_keys(self._key_target(), keys)
            return
        self._ensure_focus()
        self.inputs.send_keys(keys)

    def wheel(self, notches: int) -> None:
        if self.cfg.background:
            x, y = self.document_rect().center
            self.inputs.post_wheel(self._key_target(), x, y, notches)
            return
        self._ensure_focus()
        x, y = self.document_rect().center
        self.inputs.scroll(x, y, notches)
        self.park_cursor()

    def _wait_goto_dialog(self, timeout_s: float = 4.0) -> Optional[DialogInfo]:
        title_re = re.compile(self.cfg.goto_dialog_title_re)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            dlg = next((d for d in self.list_dialogs(None) if d.owned_by_viewer and title_re.search(d.title)), None)
            if dlg is not None:
                return dlg
            self.estop.sleep(0.08)
        return None

    def close_stray_goto_dialogs(self) -> int:
        """Close go-to-page dialogs left open (e.g. one that appeared after a timeout)."""
        if not self.cfg.goto_dialog_title_re:
            return 0
        title_re = re.compile(self.cfg.goto_dialog_title_re)
        stray = [d for d in self.list_dialogs(None) if d.owned_by_viewer and title_re.search(d.title)]
        for d in stray:
            self._close_dialog(d)
        return len(stray)

    def open_goto_dialog_and_enter(self, n: int) -> bool:
        """Go to page ``n``: open the go-to-page dialog (one shortcut), put the number into
        its edit box with WM_SETTEXT and press OK with BM_CLICK - no further key presses,
        so lost, delayed or misdirected keystrokes cannot change the page."""
        if not self.cfg.keys.goto_page or not self.cfg.goto_dialog_title_re:
            return False
        self.send_keys(self.cfg.keys.goto_page)
        dlg = self._wait_goto_dialog()
        if dlg is None:
            return False
        kids = pw.child_windows(dlg.handle)
        # Acrobat's page box is a rich-edit control (RICHEDIT50W), not a plain Edit.
        edit = next((h for h, cls, _t, _r in kids if "edit" in cls.lower()), None)
        ok_btn = self._dialog_button(kids, "ok")
        done = False
        text = self.page_labels[n - 1] if self.page_labels and n <= len(self.page_labels) else str(n)
        if edit and ok_btn and pw.set_window_text(edit, text):
            done = pw.click_dialog_command(dlg.handle, ok_btn) or pw.post_message(ok_btn, 0x00F5)
        else:
            event(self.log, "goto_dialog_controls", "edit box or OK button not found; typing instead", level=30,
                  children=[(cls, t) for _h, cls, t, _r in kids][:20])
            done = self.send_dialog_keys(dlg, "^a" + text + "{ENTER}")
        if self._wait_closed(dlg.handle, 4.0):
            return done
        # Still open (e.g. the number was rejected): close it and report failure.
        self._close_dialog(dlg)
        return False

    @staticmethod
    def _dialog_button(kids, label: str) -> Optional[int]:
        return next((h for h, cls, t, _r in kids
                     if cls == "Button" and t.replace("&", "").strip().lower() == label), None)

    def _wait_closed(self, hwnd: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not pw.is_window(hwnd) or not pw.is_visible(hwnd):
                return True
            self.estop.sleep(0.05)
        return False

    def _close_dialog(self, dlg: DialogInfo) -> bool:
        """Close a dialog without relying on keyboard focus: Cancel (BM_CLICK), then WM_CLOSE."""
        cancel = self._dialog_button(pw.child_windows(dlg.handle), "cancel")
        if cancel:
            if not pw.click_dialog_command(dlg.handle, cancel):
                pw.post_message(cancel, 0x00F5)
            if self._wait_closed(dlg.handle, 2.0):
                return True
        pw.post_message(dlg.handle, 0x0010)                          # WM_CLOSE
        if self._wait_closed(dlg.handle, 2.0):
            return True
        event(self.log, "dialog_close_failed", f"could not close {dlg.title!r}", level=30)
        return False

    def enter_page_number(self, n: int) -> bool:
        if self.cfg.background:
            return False                     # needs UIA SetFocus + a real Enter; use goto_method: dialog
        edits = [el for ct, el in (self._indicator_cache.get() or []) if ct == "Edit"]
        if not edits:
            return False
        try:
            from pywinauto.uia_defines import get_elem_interface
            edit = edits[0]
            self._ensure_focus()
            edit.SetFocus()
            get_elem_interface(edit, "Value").SetValue(str(n))
            self.inputs.send_keys("{ENTER}")
            self.estop.sleep(0.2)
            self.focus_document()
            return True
        except Exception as exc:
            event(self.log, "page_field_failed", str(exc), level=30)
            return False
