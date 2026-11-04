"""Page navigation and end-of-document detection.

End-of-document decision order (first that applies wins):

1. ``document.known_page_count`` -> stop after that page. A navigation stall
   before reaching it is a *failure* (manual checkpoint), never "the end".
2. The viewer's page indicator reports a total (e.g. "7 / 12") -> stop after
   that page. If the indicator says we are on the last page, no key is sent.
3. ``document.max_pages`` -> hard safety cap; reported as possibly incomplete.
4. Heuristic stall: a next-page key (with retries and alternative keys) does
   not change the indicator / the rendered view. If no indicator is readable,
   an *end probe* checks that navigation still works: go back one page (the
   view must change), then forward again (the view must return to the same
   image). Only then is the end "confirmed"; otherwise it is a failure.

Identical-looking consecutive pages are only accepted as distinct pages when
the page indicator proves the viewer advanced. Without an indicator the
harness cannot tell "two identical pages" from "the last page"; in that case
supply ``known_page_count``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image

from .config import HarnessConfig
from .errors import FocusLost
from .imaging import changed_fraction
from .logging_utils import event, get_logger
from .models import EndReason, Flag, NavOutcome, NavStatus, PageIndicator


@dataclass
class ViewState:
    frame: Image.Image
    indicator: PageIndicator
    stable: bool


class Navigator:
    def __init__(self, cfg: HarnessConfig, adapter, observe: Callable[[], ViewState],
                 checkpoint: Callable[[str, str], str]):
        self.cfg = cfg
        self.adapter = adapter
        self.observe = observe          # waits for stability, returns frame + indicator
        self.checkpoint = checkpoint
        self.log = get_logger()
        self.indicator_trusted = True   # flipped off if the indicator proves unreliable

    # ------------------------------------------------------------------ helpers
    def _differs(self, a: Image.Image, b: Image.Image) -> bool:
        st = self.cfg.capture.stability
        return changed_fraction(a, b, st.pixel_threshold, st.downscale) > self.cfg.capture.same_page_threshold

    def _ind(self, ind: PageIndicator) -> PageIndicator:
        return ind if self.indicator_trusted else PageIndicator(raw=ind.raw, source="untrusted")

    def _send(self, fn, *args):
        try:
            return fn(*args)
        except FocusLost as exc:
            self.checkpoint("focus_lost", f"{exc}. Click the reader window (without changing the page) "
                                          "and choose Retry.")
            return fn(*args)

    def _goto_number(self, target: int) -> bool:
        """Open the go-to-page dialog and enter ``target``; up to 3 attempts (a stray dialog
        from a failed attempt is closed first)."""
        for attempt in range(3):
            self.adapter.close_stray_goto_dialogs()
            try:
                if self.adapter.open_goto_dialog_and_enter(target):
                    return True
            except FocusLost as exc:
                event(self.log, "goto_focus_lost", f"attempt {attempt + 1}: {exc}", level=30)
            event(self.log, "goto_retry", f"go-to-page dialog failed for page {target} (attempt {attempt + 1})",
                  level=30)
        self.adapter.close_stray_goto_dialogs()
        return False

    def _advance_by_goto(self, page_no: int, prev: ViewState) -> Optional[NavOutcome]:
        """Go to page ``page_no + 1`` by number (go-to-page dialog). None if the dialog
        could not be used, so the caller falls back to key presses."""
        target = page_no + 1
        use_indicator = not getattr(self.adapter, "page_labels", None)   # labels: indicator shows labels
        for attempt in range(2):
            if not self._goto_number(target):
                event(self.log, "goto_unavailable", f"go-to-page dialog failed for page {target}", level=30)
                return None
            cur = self.observe()
            changed = self._differs(prev.frame, cur.frame)
            ind = self._ind(cur.indicator)
            event(self.log, "nav_attempt", page=page_no, attempt=attempt + 1, method="goto", target=target,
                  changed=changed, current=ind.current, level=10, **self.adapter.focus_info())
            if use_indicator and ind.readable and ind.current != target:
                continue                                   # landed elsewhere: try again
            if changed:
                return NavOutcome(NavStatus.ADVANCED, f"went to page {target} by number", attempt + 1, ind)
        # The dialog was used twice and the view is unchanged: either two identical pages
        # or the jump did not happen. Accept it, but flag the page for a human to check.
        return NavOutcome(NavStatus.ADVANCED, f"page {target} looks identical to page {page_no}", 2,
                          self._ind(cur.indicator), [Flag.IDENTICAL_PREV, Flag.NAV_RETRY_UNVERIFIED])

    # ------------------------------------------------------------------ advance
    def advance(self, page_no: int, prev: ViewState, known_total: Optional[int],
                goto_total: Optional[int] = None) -> NavOutcome:
        """Move from ``page_no`` to ``page_no + 1`` and verify the move. ``goto_total``
        (any known page count, including the reader's indicator) enables page-number
        navigation; ``known_total`` additionally makes a stall a failure."""
        retries = self.cfg.capture.nav_retries
        goto_total = goto_total if goto_total is not None else known_total
        if self.cfg.viewer.navigate_by == "goto" and goto_total is not None and page_no < goto_total:
            out = self._advance_by_goto(page_no, prev)
            if out is not None:
                return out
        prev_ind = self._ind(prev.indicator)
        last: Optional[ViewState] = None
        for attempt in range(retries + 1):
            self._send(self.adapter.next_page, attempt)
            cur = self.observe()
            last = cur
            ind = self._ind(cur.indicator)
            changed = self._differs(prev.frame, cur.frame)
            event(self.log, "nav_attempt", page=page_no, attempt=attempt + 1, changed=changed,
                  indicator=ind.raw, current=ind.current, total=ind.total, level=10,
                  **self.adapter.focus_info())
            if prev_ind.readable and ind.readable:
                if ind.current == prev_ind.current + 1:
                    flags = [] if changed else [Flag.IDENTICAL_PREV]
                    return NavOutcome(NavStatus.ADVANCED, "indicator incremented", attempt + 1, ind, flags)
                if ind.current > prev_ind.current + 1:
                    # Overshoot (double key press, two-up layout): step back once.
                    self._send(self.adapter.prev_page)
                    back = self.observe()
                    bind = self._ind(back.indicator)
                    if bind.readable and bind.current == prev_ind.current + 1:
                        return NavOutcome(NavStatus.ADVANCED, "overshoot corrected", attempt + 2, bind,
                                          [Flag.INDICATOR_MISMATCH])
                    return NavOutcome(NavStatus.FAILED, f"indicator jumped {prev_ind.current}->{ind.current}",
                                      attempt + 1, ind)
                if ind.current == prev_ind.current:
                    total = ind.total or prev_ind.total
                    if total is not None and ind.current >= total and known_total is None:
                        return NavOutcome(NavStatus.END, "indicator at last page", attempt + 1, ind,
                                          end_reason=EndReason.INDICATOR_TOTAL)
                    continue
                # Indicator went backwards: treat as failure.
                return NavOutcome(NavStatus.FAILED, f"indicator went back {prev_ind.current}->{ind.current}",
                                  attempt + 1, ind)
            # No usable indicator: rely on the rendered view.
            if changed:
                fl = [Flag.NO_INDICATOR] + ([Flag.NAV_RETRY_UNVERIFIED] if attempt else [])
                return NavOutcome(NavStatus.ADVANCED, "view changed (no indicator)", attempt + 1, ind, fl)
        assert last is not None
        attempts = retries + 1
        if prev_ind.readable and self._ind(last.indicator).readable:
            return NavOutcome(NavStatus.FAILED, "indicator did not advance", attempts, last.indicator)
        if known_total is not None and page_no < known_total:
            return NavOutcome(NavStatus.FAILED,
                              f"view did not change but known_page_count={known_total}", attempts, last.indicator)
        if not self.cfg.capture.end_probe:
            return NavOutcome(NavStatus.END, "view unchanged after retries (probe disabled)", attempts,
                              last.indicator, end_reason=EndReason.STALL_UNCONFIRMED)
        return self._end_probe(page_no, prev, attempts)

    def _end_probe(self, page_no: int, at_end: ViewState, attempts: int) -> NavOutcome:
        """Confirm we are really at the end: back must change the view, forward must restore it."""
        self._send(self.adapter.prev_page)
        back = self.observe()
        if not self._differs(at_end.frame, back.frame):
            if page_no == 1:
                return NavOutcome(NavStatus.END, "single page, or navigation unresponsive", attempts + 1,
                                  back.indicator, end_reason=EndReason.STALL_UNCONFIRMED)
            return NavOutcome(NavStatus.FAILED, "navigation unresponsive (prev and next both had no effect)",
                              attempts + 1, back.indicator)
        self._send(self.adapter.next_page, 0)
        fwd = self.observe()
        if self._differs(at_end.frame, fwd.frame):
            return NavOutcome(NavStatus.FAILED, "end probe inconsistent: returning forward did not restore view",
                              attempts + 2, fwd.indicator)
        return NavOutcome(NavStatus.END, "next has no effect; prev/next probe confirms navigation works",
                          attempts + 2, fwd.indicator, end_reason=EndReason.STALL_CONFIRMED)

    # ------------------------------------------------------------------ goto
    def goto(self, n: int, verify: Optional[Callable[[ViewState], bool]] = None) -> tuple[bool, ViewState]:
        """Navigate to page ``n`` (1-based). Returns (verified, state). ``verify``
        confirms the page without a readable page number (e.g. by its saved image)."""
        method = self.cfg.viewer.goto_method
        if n == 1:
            if not (method == "dialog" and self._goto_number(1)):
                self._send(self.adapter.first_page)
            st = self.observe()
            ind = self._ind(st.indicator)
            return (ind.current == 1 if ind.readable else True), st
        jumped = False
        if method == "dialog" and self._goto_number(n):
            st = self.observe()
            jumped = True
        elif method == "page_field" and self.adapter.enter_page_number(n):
            st = self.observe()
        elif method == "manual":
            self.checkpoint("goto_page", f"Navigate the reader to page {n} and choose Retry.")
            st = self.observe()
        else:
            if method in ("dialog", "page_field"):
                event(self.log, "goto_fallback_sequential", f"goto via {method} failed; stepping from page 1",
                      level=30)
            ok, st = self.goto(1)
            for p in range(1, n):
                out = self.advance(p, st, None)
                if out.status != NavStatus.ADVANCED:
                    return False, self.observe()
                st = self.observe()
            ind = self._ind(st.indicator)
            return (ind.current == n if ind.readable else True), st
        ind = self._ind(st.indicator)
        if ind.readable:
            return ind.current == n, st
        if verify is not None and verify(st):
            return True, st
        if jumped:
            # The go-to-page dialog accepted the number and closed: the reader is on page n.
            # (An image check can fail when the saved page came from an older version.)
            event(self.log, "goto_trusted", f"on page {n} by number (image check not conclusive)", level=30)
            return True, st
        # Cannot verify without an indicator.
        ans = self.checkpoint("verify_page",
                              f"The harness cannot read the page number. Confirm the reader shows page {n} "
                              "(navigate there if needed) and choose Retry/Continue.")
        return ans in ("retry", "continue"), self.observe()
