"""A simulated reader for testing the runner without Windows.

It renders synthetic pages onto a grey canvas, simulates progressive loading,
dialogs, flaky navigation and an optional page indicator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

from doc_capture_harness.models import DialogInfo, PageIndicator, Rect
from doc_capture_harness.viewers.base import CaptureSource, ViewerAdapter

CANVAS = (82, 82, 82)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(size: int):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default(size=size)


WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
         "november oscar papa quebec romeo sierra tango uniform victor whiskey").split()


def make_page(n: int, w: int = 600, h: int = 780, label: Optional[str] = None, lines: int = 12) -> Image.Image:
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 30), label or f"Page {n}", fill="black", font=font(40))
    f = font(24)
    y = 110
    for i in range(lines):
        words = [WORDS[(n * 7 + i * 3 + k) % len(WORDS)] for k in range(5)]
        d.text((40, y), f"{i + 1}. " + " ".join(words), fill="black", font=f)
        y += (h - 150) // lines
    return img


@dataclass
class FakeDialog:
    title: str
    texts: list[str]
    buttons: list[str]
    owned: bool = True
    handle: int = 0


@dataclass
class Scenario:
    pages: list[Image.Image]
    indicator: bool = True
    loading_grabs: int = 2                  # grabs showing a partial render after each navigation
    viewport: tuple[int, int] = (800, 900)
    # page number -> dialog that appears when the page is first shown
    dialogs_on_page: dict[int, FakeDialog] = field(default_factory=dict)
    # page number -> how many next_page presses are ignored there
    stuck_next: dict[int, int] = field(default_factory=dict)
    fit: str = "page"                       # page | width (scroll mode)
    layout: str = "single"                  # single | continuous (two pages visible) | zoomed (page cut off)
    overlays: list = field(default_factory=list)   # reader UI drawn over the page (e.g. "Ask AI Assistant")
    on_page_shown: Optional[Callable[[int], None]] = None


class FakeViewer(ViewerAdapter):
    name = "fake"

    def __init__(self, scenario: Scenario, cfg, estop, checkpoint):
        super().__init__(cfg, estop, checkpoint)
        self.s = scenario
        self.page = 0           # 0-based
        self.scroll = 0
        self.loading = 0
        self.dialogs: list[FakeDialog] = []
        self.shown: set[int] = set()
        self.next_handle = 1000
        self.launched: Optional[Path] = None
        self.keys_sent: list[str] = []
        self._show()

    # -- lifecycle
    def launch(self, document: Path) -> None:
        self.launched = document

    def attach(self, document: Path, timeout_s: float) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def main_handle(self) -> int:
        return 1

    def window_rect(self) -> Rect:
        return Rect(0, 0, self.s.viewport[0] + 200, self.s.viewport[1] + 200)

    def document_rect(self) -> Rect:
        return Rect(100, 150, 100 + self.s.viewport[0], 150 + self.s.viewport[1])

    def is_foreground(self) -> bool:
        return True

    def bring_to_foreground(self) -> bool:
        return True

    def is_busy(self) -> Optional[bool]:
        return self.loading > 0 and self.loading % 2 == 0

    def capture_protected(self) -> bool:
        return False

    def read_page_indicator(self) -> PageIndicator:
        if not self.s.indicator:
            return PageIndicator()
        return PageIndicator(self.page + 1, len(self.s.pages), f"{self.page + 1} / {len(self.s.pages)}", "uia")

    def environment_info(self) -> dict:
        return {"os": "simulated"}

    def find_overlays(self, doc_rect):
        return list(self.s.overlays)

    # -- dialogs
    def list_dialogs(self, doc_rect):
        out = []
        for d in self.dialogs:
            out.append(DialogInfo(d.handle, d.title, "#32770", 42, Rect(200, 200, 500, 400),
                                  d.texts, d.buttons, d.owned))
        return out

    def click_dialog_button(self, dialog, button) -> bool:
        for d in list(self.dialogs):
            if d.handle == dialog.handle and button in d.buttons:
                self.dialogs.remove(d)
                return True
        return False

    def send_dialog_keys(self, dialog, keys) -> bool:
        return False

    def dismiss_all(self):
        self.dialogs.clear()

    # -- input
    def send_keys(self, keys: str) -> None:
        self.estop.check()
        self.keys_sent.append(keys)
        k = self.cfg.keys
        if keys in (k.next_page, *k.alt_next_page):
            left = self.s.stuck_next.get(self.page + 1, 0)
            if left > 0:
                self.s.stuck_next[self.page + 1] = left - 1
                return
            if self.page < len(self.s.pages) - 1:
                self.page += 1
                self.scroll = 0
                self._show()
        elif keys == k.prev_page:
            if self.page > 0:
                self.page -= 1
                self.scroll = 0
                self._show()
        elif keys == k.first_page:
            self.page = 0
            self.scroll = 0
            self._show()

    def wheel(self, notches: int) -> None:
        self.estop.check()
        page = self._scaled_page()
        max_scroll = max(0, page.height - self.s.viewport[1])
        self.scroll = max(0, min(max_scroll, self.scroll - notches * 60))

    def _show(self):
        self.loading = self.s.loading_grabs
        n = self.page + 1
        if n not in self.shown:
            self.shown.add(n)
            if n in self.s.dialogs_on_page:
                d = self.s.dialogs_on_page[n]
                self.next_handle += 1
                d.handle = self.next_handle
                self.dialogs.append(d)
            if self.s.on_page_shown:
                self.s.on_page_shown(n)

    # -- rendering
    def _scaled_page(self) -> Image.Image:
        p = self.s.pages[self.page]
        vw, vh = self.s.viewport
        if self.s.fit == "width":
            scale = (vw - 40) / p.width
        else:
            scale = min((vw - 40) / p.width, (vh - 40) / p.height)
        return p.resize((int(p.width * scale), int(p.height * scale)), Image.LANCZOS)

    def render(self) -> Image.Image:
        vw, vh = self.s.viewport
        img = Image.new("RGB", (vw, vh), CANVAS)
        page = self._scaled_page()
        if self.loading > 0:
            # Progressive render: only the top part is drawn yet.
            self.loading -= 1
            part = page.crop((0, 0, page.width, page.height // 3))
            page = Image.new("RGB", page.size, "white")
            page.paste(part, (0, 0))
        x = (vw - page.width) // 2
        if self.s.layout == "continuous":
            small = page.resize((page.width * 2 // 3, page.height * 2 // 3))
            x = (vw - small.width) // 2
            img.paste(small, (x, 10))
            img.paste(small, (x, 10 + small.height + 20))      # next page peeking in below
            return img
        if self.s.layout == "zoomed":
            big = page.resize((int(page.width * 1.15), int(page.height * 1.15)))   # taller than the view
            img.paste(big, ((vw - big.width) // 2, 0))
            return img
        if self.s.fit == "width":
            img.paste(page.crop((0, self.scroll, page.width, min(page.height, self.scroll + vh))), (x, 0))
        else:
            img.paste(page, (x, (vh - page.height) // 2))
        return img


class FakeSource(CaptureSource):
    def __init__(self, viewer: FakeViewer):
        self.v = viewer
        self.grabs = 0

    def grab(self, rect: Rect) -> Image.Image:
        self.grabs += 1
        if rect == self.v.document_rect():
            return self.v.render()
        # Window / other rect: just paint a frame with the viewport inside.
        img = Image.new("RGB", (rect.width, rect.height), (230, 230, 230))
        dr = self.v.document_rect()
        img.paste(self.v.render(), (dr.left - rect.left, dr.top - rect.top))
        return img
