"""Viewer adapter interface.

A *viewer adapter* knows how to drive one desktop reader: launch it, find its
window and document pane, send navigation keys, read its page indicator,
report whether it is busy, and enumerate/handle its dialogs. It knows nothing
about stability checks, cropping, OCR, manifests or reports; those live in
the runner and are shared by every reader.

To support a new reader, either configure ``profile: generic`` (all behaviour
from config) or subclass :class:`Win32ViewerAdapter` and override what
differs (see ``acrobat.py`` / ``pdfxchange.py``).
"""
from __future__ import annotations

import abc
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from ..config import ViewerConfig
from ..estop import EmergencyStopMonitor
from ..models import DialogInfo, PageIndicator, Rect

# Checkpoint callback: (reason, message) -> "retry" | "continue" | "skip" | raises UserAbort
CheckpointFn = Callable[[str, str], str]


class ViewerAdapter(abc.ABC):
    name = "abstract"

    def __init__(self, cfg: ViewerConfig, estop: EmergencyStopMonitor, checkpoint: CheckpointFn):
        self.cfg = cfg
        self.estop = estop
        self.checkpoint = checkpoint

    # -- lifecycle -------------------------------------------------------------
    @abc.abstractmethod
    def launch(self, document: Path) -> None: ...

    @abc.abstractmethod
    def attach(self, document: Path, timeout_s: float) -> None: ...

    @abc.abstractmethod
    def is_alive(self) -> bool: ...

    def close(self) -> None:
        """Close the document window (only called if viewer.close_on_finish)."""

    # -- geometry / state ------------------------------------------------------
    @abc.abstractmethod
    def main_handle(self) -> int: ...

    @abc.abstractmethod
    def window_rect(self) -> Rect: ...

    @abc.abstractmethod
    def document_rect(self) -> Rect:
        """Screen rectangle of the document viewport (physical pixels)."""

    @abc.abstractmethod
    def is_foreground(self) -> bool: ...

    @abc.abstractmethod
    def bring_to_foreground(self) -> bool: ...

    @abc.abstractmethod
    def is_busy(self) -> Optional[bool]: ...

    @abc.abstractmethod
    def capture_protected(self) -> bool:
        """True if the window asks the OS to exclude it from screen capture."""

    @abc.abstractmethod
    def read_page_indicator(self) -> PageIndicator: ...

    def environment_info(self) -> dict:
        return {}

    def screen_layout(self, rect: Rect) -> Optional[tuple[Rect, list]]:
        """(virtual desktop rect, monitors overlapping ``rect``) or None if unknown.

        Monitors need ``device`` and ``scale`` attributes.
        """
        return None

    def page_count_hint(self) -> Optional[int]:
        """Total page count as shown by the reader's own UI (e.g. a go-to-page
        dialog), read once before capture; None if not available."""
        return None

    def focus_info(self) -> dict:
        """Which window / UI element currently has keyboard focus (for diagnostics)."""
        return {}

    def find_overlays(self, doc_rect: Optional[Rect]) -> list[str]:
        """Names of known reader UI elements drawn over the document area
        (``viewer.overlay_name_re``), e.g. Acrobat's AI Assistant bar."""
        return []

    def park_cursor(self) -> None:
        """Move the mouse pointer off the document so hover effects are not captured."""

    def grab_window(self) -> Optional[Image.Image]:
        """PrintWindow-style capture of the whole main window (optional)."""
        return None

    # -- dialogs ---------------------------------------------------------------
    @abc.abstractmethod
    def list_dialogs(self, doc_rect: Optional[Rect]) -> list[DialogInfo]:
        """Viewer-owned dialogs plus foreign windows that obstruct ``doc_rect``."""

    @abc.abstractmethod
    def click_dialog_button(self, dialog: DialogInfo, button: str) -> bool: ...

    @abc.abstractmethod
    def send_dialog_keys(self, dialog: DialogInfo, keys: str) -> bool: ...

    # -- input -----------------------------------------------------------------
    @abc.abstractmethod
    def send_keys(self, keys: str) -> None:
        """Send keys to the document view (after verifying focus)."""

    @abc.abstractmethod
    def wheel(self, notches: int) -> None:
        """Mouse-wheel over the document view; negative scrolls down."""

    def focus_document(self) -> None:
        """Give keyboard focus to the document pane, if possible."""

    def enter_page_number(self, n: int) -> bool:
        """Type ``n`` into the viewer's page-number field (goto_method=page_field)."""
        return False

    def open_goto_dialog_and_enter(self, n: int) -> bool:
        """Open the viewer's go-to-page dialog and submit ``n`` (goto_method=dialog)."""
        return False

    # -- navigation primitives built on keys -------------------------------------
    def prepare_view(self) -> None:
        for k in self.cfg.prepare_keys:
            self.send_keys(k)
            self.estop.sleep(0.3)

    def next_page(self, attempt: int = 0) -> None:
        keys = [self.cfg.keys.next_page, *self.cfg.keys.alt_next_page]
        self.send_keys(keys[min(attempt, len(keys) - 1)])

    def prev_page(self) -> None:
        self.send_keys(self.cfg.keys.prev_page)

    def first_page(self) -> None:
        self.send_keys(self.cfg.keys.first_page)

    def scroll_down(self, notches: int) -> None:
        if self.cfg.keys.scroll_down:
            self.send_keys(self.cfg.keys.scroll_down)
        else:
            self.wheel(-abs(notches))

    def scroll_up(self, notches: int) -> None:
        if self.cfg.keys.scroll_up:
            self.send_keys(self.cfg.keys.scroll_up)
        else:
            self.wheel(abs(notches))


class CaptureSource(abc.ABC):
    """Produces an image of a screen rectangle."""

    @abc.abstractmethod
    def grab(self, rect: Rect) -> Image.Image: ...

    def close(self) -> None:
        pass


class MssCaptureSource(CaptureSource):
    """Desktop capture with ``mss`` (works across monitors, negative coordinates OK).

    Captures exactly what is on screen, so anything covering the viewport is
    captured too; the runner checks for obstructions before each capture.
    """

    def __init__(self):
        import mss
        self._mss = mss.mss()

    def grab(self, rect: Rect) -> Image.Image:
        shot = self._mss.grab({"left": rect.left, "top": rect.top, "width": rect.width, "height": rect.height})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def close(self) -> None:
        self._mss.close()


class PrintWindowCaptureSource(CaptureSource):
    """Capture via ``PrintWindow(PW_RENDERFULLCONTENT)``; works while occluded,
    but some GPU-accelerated viewers return black frames (detected as
    ``all_black_capture``)."""

    def __init__(self, adapter: ViewerAdapter):
        self.adapter = adapter

    def grab(self, rect: Rect) -> Image.Image:
        img = self.adapter.grab_window()
        if img is None:
            raise RuntimeError("PrintWindow capture not supported by this adapter")
        wr = self.adapter.window_rect()
        return img.crop((rect.left - wr.left, rect.top - wr.top, rect.right - wr.left, rect.bottom - wr.top))
