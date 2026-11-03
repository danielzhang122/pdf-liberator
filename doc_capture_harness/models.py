"""Plain data types shared by viewer adapters, capture, OCR and reporting.

Nothing in this module touches the OS, so it is importable (and testable) on
any platform.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass(frozen=True)
class Rect:
    """Screen rectangle in *physical* pixels (the harness is DPI-aware)."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def is_empty(self) -> bool:
        return self.width == 0 or self.height == 0

    def intersect(self, other: "Rect") -> "Rect":
        r = Rect(max(self.left, other.left), max(self.top, other.top),
                 min(self.right, other.right), min(self.bottom, other.bottom))
        return r if r.right > r.left and r.bottom > r.top else Rect(0, 0, 0, 0)

    def intersects(self, other: "Rect") -> bool:
        return not self.intersect(other).is_empty()

    def inset(self, left: int = 0, top: int = 0, right: int = 0, bottom: int = 0) -> "Rect":
        return Rect(self.left + left, self.top + top, self.right - right, self.bottom - bottom)

    def to_dict(self) -> dict:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}

    def as_list(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]


@dataclass
class DialogInfo:
    """A top-level window that is not the viewer's main document window."""

    handle: int
    title: str
    class_name: str
    process_id: int
    rect: Rect
    texts: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    owned_by_viewer: bool = True   # False => some other app's window overlapping the document

    def all_text(self) -> str:
        return "\n".join([self.title, *self.texts, *self.buttons])

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rect"] = self.rect.to_dict()
        return d


@dataclass
class PageIndicator:
    """What the viewer UI says about the current page, if readable."""

    current: Optional[int] = None
    total: Optional[int] = None
    raw: str = ""
    source: str = "none"   # uia | ocr | none

    @property
    def readable(self) -> bool:
        return self.current is not None


class EndReason(str, enum.Enum):
    KNOWN_PAGE_COUNT = "known_page_count_reached"
    INDICATOR_TOTAL = "viewer_reported_total_reached"
    MAX_PAGES = "max_pages_limit_reached"
    STALL_CONFIRMED = "navigation_stalled_end_confirmed_by_probe"
    STALL_UNCONFIRMED = "navigation_stalled_end_not_confirmed"
    NAVIGATION_FAILED = "navigation_failed"
    USER_ABORT = "user_abort"
    EMERGENCY_STOP = "emergency_stop"
    ERROR = "error"


class NavStatus(str, enum.Enum):
    ADVANCED = "advanced"
    END = "end"
    FAILED = "failed"


@dataclass
class NavOutcome:
    status: NavStatus
    reason: str = ""
    attempts: int = 0
    indicator: PageIndicator = field(default_factory=PageIndicator)
    flags: list[str] = field(default_factory=list)
    end_reason: Optional[EndReason] = None


class Flag:
    """Page-level flags written to the manifest and report."""

    UNSTABLE = "render_not_stable"
    BLANK = "blank_or_uniform"
    ALL_BLACK = "all_black_capture"
    PLACEHOLDER = "placeholder_text_detected"
    IDENTICAL_PREV = "identical_to_previous_page"
    DUPLICATE = "duplicate_of_other_page"
    STITCH_UNCERTAIN = "stitch_overlap_uncertain"
    SLICES_TRUNCATED = "max_scroll_slices_reached"
    BOUNDARY_CROSSED = "scroll_crossed_page_boundary"
    INDICATOR_MISMATCH = "page_indicator_mismatch"
    NO_INDICATOR = "advance_not_verified_by_page_indicator"
    # Advanced only on a retry with no indicator: an earlier key press may have
    # moved to an identical-looking page, so a page could have been skipped.
    NAV_RETRY_UNVERIFIED = "advance_after_retry_unverified"
    OBSTRUCTED = "obstruction_detected"
    LOW_CONFIDENCE = "low_ocr_confidence"
    LOW_RESOLUTION = "text_too_small_for_reliable_ocr"
    NO_TEXT = "no_text_recognised"
    CROP_FALLBACK = "page_crop_fallback_to_viewport"
    MANUAL_OVERRIDE = "manual_override_used"
    CAPTURE_FAILED = "capture_failed"


# Flags that mean "a human should look at this page".
INCOMPLETE_FLAGS = {
    Flag.UNSTABLE, Flag.ALL_BLACK, Flag.PLACEHOLDER, Flag.STITCH_UNCERTAIN,
    Flag.SLICES_TRUNCATED, Flag.INDICATOR_MISMATCH, Flag.OBSTRUCTED,
    Flag.LOW_CONFIDENCE, Flag.LOW_RESOLUTION, Flag.CROP_FALLBACK, Flag.CAPTURE_FAILED,
    Flag.MANUAL_OVERRIDE, Flag.NAV_RETRY_UNVERIFIED,
}
