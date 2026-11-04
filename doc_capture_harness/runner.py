"""Orchestration: open -> wait for load -> capture pages -> OCR -> report.

The runner is reader-agnostic: it talks to a :class:`ViewerAdapter` and a
:class:`CaptureSource`. On Windows the CLI wires in the pywinauto adapters and
``mss``; the test-suite wires in a simulated viewer.
"""
from __future__ import annotations

import collections
import contextlib
import os
import shutil
import stat
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from . import __version__
from .config import HarnessConfig
from .errors import (CaptureBlocked, EmergencyStop, FocusLost, HarnessError, ResumeMismatch,
                     UserAbort)
from .estop import EmergencyStopMonitor
from .imaging import (changed_fraction, crop_to_page, dhash, hamming, is_all_black, is_uniform,
                      layout_diagnostics, layout_issues, sha256_image, stitch_vertical)
from .logging_utils import event, setup_logging
from .manual import ManualCheckpoint
from .models import EndReason, Flag, NavStatus, PageIndicator, Rect
from .navigation import Navigator, ViewState
from .ocr_pipeline import OcrPipeline
from .prompts import PromptHandler
from .stability import wait_for_stable
from .state import Manifest, RunPaths, atomic_write_json, now_iso, sha256_file
from .viewers.base import CaptureSource, ViewerAdapter

AdapterFactory = Callable[[Callable[[str, str], str], EmergencyStopMonitor], ViewerAdapter]
SourceFactory = Callable[[ViewerAdapter], CaptureSource]

EXIT_OK, EXIT_WARNINGS, EXIT_STOPPED, EXIT_FAILED = 0, 2, 3, 4


@dataclass
class PageCapture:
    image: Optional[Image.Image]
    raw_frame: Optional[Image.Image]
    indicator: PageIndicator
    flags: list[str]
    attempts: int
    retry_reasons: list[str]
    info: dict


class Harness:
    def __init__(self, cfg: HarnessConfig, run_dir: Path, adapter_factory: AdapterFactory,
                 source_factory: SourceFactory, *, resume: bool = False, force: bool = False,
                 responder: Optional[Callable[[str, str], str]] = None, skip_ocr: bool = False,
                 skip_pdf: bool = False, extra_stop_files: tuple = ()):
        self.cfg = cfg
        self.paths = RunPaths(Path(run_dir))
        self.paths.ensure()
        self.log = setup_logging(self.paths.logs, cfg.logging.level, cfg.logging.console)
        self.adapter_factory = adapter_factory
        self.source_factory = source_factory
        self.resume = resume
        self.force = force
        self.skip_ocr = skip_ocr
        self.skip_pdf = skip_pdf
        self.estop = EmergencyStopMonitor(cfg.safety.estop_hotkey, self.paths.root / cfg.safety.stop_file_name,
                                          extra_stop_files=tuple(extra_stop_files))
        self.manifest: Optional[Manifest] = None
        self.checkpoint = ManualCheckpoint(cfg.manual, self.estop, diagnostics=self.save_diagnostics,
                                           record=lambda r: self._record_event("manual_checkpoints", r),
                                           responder=responder)
        self.adapter: Optional[ViewerAdapter] = None
        self.source: Optional[CaptureSource] = None
        self.prompts: Optional[PromptHandler] = None
        self.navigator: Optional[Navigator] = None
        self._hashes: dict[int, int] = {}
        self.pipeline: Optional[OcrPipeline] = None
        self._orig_info = None
        self.deadline: Optional[float] = None
        self._first_checkpoint_done = False
        self._timers: dict[str, float] = collections.defaultdict(float)
        self._page_region = None
        self._page_count_hint: Optional[int] = None
        self._placeholder_ocr_ok: Optional[bool] = None

    # ================================================================== helpers
    def _record_event(self, kind: str, payload: dict) -> None:
        if self.manifest is not None:
            self.manifest.add_event(kind, payload)

    def save_diagnostics(self, reason: str) -> dict:
        """Screenshot(s) + JSON state; never raises."""
        ts = time.strftime("%Y%m%d-%H%M%S")
        base = self.paths.diagnostics / f"{ts}_{reason}"
        out: dict = {"prefix": str(base.name)}
        info: dict = {"reason": reason, "ts": now_iso()}
        try:
            if self.adapter is not None and self.source is not None and self.adapter.is_alive():
                wr = self.adapter.window_rect()
                info["window_rect"] = wr.to_dict()
                try:
                    dr = self.adapter.document_rect()
                    info["document_rect"] = dr.to_dict()
                except Exception as exc:
                    info["document_rect_error"] = str(exc)
                rect = wr
                if self.cfg.output.diagnostics_full_screen:
                    try:
                        from . import platform_win as pw
                        rect = pw.virtual_screen()
                    except Exception:
                        pass
                img = self.source.grab(rect)
                img.save(f"{base}_screen.png")
                out["screenshot"] = f"{base.name}_screen.png"
                try:
                    info["dialogs"] = [d.to_dict() for d in self.adapter.list_dialogs(None)]
                except Exception as exc:
                    info["dialogs_error"] = str(exc)
                try:
                    ind = self._indicator()
                    info["indicator"] = {"current": ind.current, "total": ind.total, "raw": ind.raw}
                except Exception as exc:
                    info["indicator_error"] = str(exc)
                try:
                    info["focus"] = self.adapter.focus_info()
                except Exception as exc:
                    info["focus_error"] = str(exc)
        except Exception as exc:
            info["capture_error"] = str(exc)
        try:
            atomic_write_json(Path(f"{base}.json"), info)
            out["json"] = f"{base.name}.json"
        except Exception:
            pass
        return out

    def _sleep(self, s: float) -> None:
        self.estop.sleep(s)

    # ================================================================== document
    def _prepare_document(self) -> Path:
        src = Path(self.cfg.document.path).expanduser().resolve()
        if not src.is_file():
            raise HarnessError(f"document not found: {src}")
        st = src.stat()
        digest = sha256_file(src)
        doc = {"path": str(src), "name": src.name, "size": st.st_size, "mtime": st.st_mtime,
               "sha256": digest, "open_copy": self.cfg.document.open_copy}
        if self.resume and self.paths.manifest.exists():
            loaded = Manifest.load(self.paths)
            old = loaded.data["document"]
            if old["sha256"] != digest and not self.force:
                # Leave the existing run untouched.
                raise ResumeMismatch(f"document hash changed since the run started ({old['sha256'][:12]} -> "
                                     f"{digest[:12]}); use --force to resume anyway")
            self.manifest = loaded
            if self.manifest.data["config_fingerprint"] != self.cfg.fingerprint():
                event(self.log, "resume_config_changed",
                      "viewer/capture settings differ from the original run", level=30)
            self.manifest.data["status"] = "running"
            self.manifest.data["end"] = None
        else:
            if self.paths.manifest.exists() and not self.force:
                raise HarnessError(f"{self.paths.manifest} exists; use `resume` or choose another run directory")
            self.manifest = Manifest.create(self.paths, document=doc, viewer=self.cfg.viewer.profile,
                                            config_fingerprint=self.cfg.fingerprint(),
                                            run_id=self.paths.root.name or uuid.uuid4().hex[:8])
            atomic_write_json(self.paths.root / "config.resolved.json", self.cfg.to_dict())
        self.manifest.data["sessions"].append({"started": now_iso(), "resume": self.resume,
                                               "harness_version": __version__})
        self.manifest.save()
        if not self.cfg.document.open_copy:
            return src
        # Open a read-only copy so the reader can never write to the original.
        self.paths.work.mkdir(parents=True, exist_ok=True)
        copy = self.paths.work / src.name
        if not copy.exists() or sha256_file(copy) != digest:
            if copy.exists():
                os.chmod(copy, stat.S_IWRITE | stat.S_IREAD)
                copy.unlink()
            shutil.copy2(src, copy)
        os.chmod(copy, stat.S_IREAD)
        return copy

    def _verify_original(self) -> dict:
        src = Path(self.manifest.data["document"]["path"])
        try:
            now = sha256_file(src)
        except OSError as exc:
            return {"checked": False, "error": str(exc)}
        ok = now == self.manifest.data["document"]["sha256"]
        if not ok:
            event(self.log, "original_modified", "ORIGINAL DOCUMENT HASH CHANGED DURING RUN", level=40)
        return {"checked": True, "unchanged": ok, "sha256_after": now}

    # ================================================================== observe
    def _doc_rect(self) -> Rect:
        r = self.adapter.document_rect()
        if r.width < 50 or r.height < 50:
            raise HarnessError(f"document viewport too small: {r}")
        return r

    def _check_rect_on_screen(self, r: Rect) -> None:
        layout = self.adapter.screen_layout(r)
        if layout is None:
            return
        vs, mons = layout
        if r.intersect(vs) != r:
            self.checkpoint("offscreen", f"The document area {r.to_dict()} is partly outside the desktop "
                                         f"{vs.to_dict()}. Move/maximize the reader window, then Retry.")
        scales = {m.scale for m in mons}
        if len(mons) > 1:
            event(self.log, "multi_monitor_span", "document area spans several monitors", level=30,
                  monitors=[m.device for m in mons], scales=sorted(s for s in scales if s))
            if len(scales) > 1:
                self.checkpoint("mixed_dpi", "The reader window spans monitors with different scaling. "
                                             "Move it fully onto one monitor, then Retry.")

    @contextlib.contextmanager
    def _timed(self, name: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            self._timers[name] += time.monotonic() - t0

    def _set_page_region(self, box, view_size) -> None:
        """Remember where the page sits in the view, so stability is judged on the page only."""
        if box:
            l, t, r, b = box
            self._page_region = ((l - 12, t - 12, r + 12, b + 12), tuple(view_size))

    def _stable(self, rect: Optional[Rect] = None, timeout: Optional[float] = None):
        rect = rect or self._doc_rect()
        self.adapter.park_cursor()
        region = None
        if self._page_region and self._page_region[1] == (rect.width, rect.height):
            region = self._page_region[0]
        with self._timed("render_wait"):
            return wait_for_stable(lambda: self.source.grab(rect), self.cfg.capture.stability,
                                   sleep=self._sleep, is_busy=self.adapter.is_busy, timeout_s=timeout,
                                   region=region)

    def _indicator(self) -> PageIndicator:
        with self._timed("page_number"):
            return self.adapter.read_page_indicator()

    def _dialogs(self, rect: Optional[Rect]) -> int:
        with self._timed("dialog_checks"):
            return self.prompts.handle(rect)

    def _observe(self) -> ViewState:
        res = self._stable()
        ind = self._indicator()
        return ViewState(res.frame, ind, res.stable)

    def _settle(self) -> bool:
        """Deal with anything that appeared after a key press (dialogs, other windows over
        the page, the reader losing focus). True if something had to be dealt with."""
        intervened = bool(self._dialogs(self._safe_doc_rect()))
        try:
            if not self.adapter.is_foreground():
                event(self.log, "focus_stolen", "another window took the focus; taking it back", level=30,
                      **self.adapter.focus_info())
                self.adapter.bring_to_foreground()
                intervened = True
        except Exception as exc:  # noqa: BLE001 - best effort
            event(self.log, "focus_check_failed", str(exc), level=10)
        return intervened

    def _observe_settled(self) -> ViewState:
        """Observe the view after an input. If a pop-up or focus change intervened, it is
        handled first and the view observed again, so a pop-up appearing (or vanishing) is
        never mistaken for a page turn."""
        st = self._observe()
        for _ in range(3):
            if not self._settle():
                return st
            st = self._observe()
        return st

    def _placeholder(self, img: Image.Image) -> Optional[str]:
        pats = self.cfg.capture.placeholder_text_re
        if not pats:
            return None
        if self._placeholder_ocr_ok is None:
            try:
                from .ocr import resolve_tesseract
                resolve_tesseract(self.cfg.ocr)
                self._placeholder_ocr_ok = True
            except Exception as exc:
                event(self.log, "placeholder_check_disabled", f"tesseract unavailable: {exc}", level=30)
                self._placeholder_ocr_ok = False
        if not self._placeholder_ocr_ok:
            return None
        import re
        from .ocr import quick_text
        text = quick_text(img, self.cfg.ocr)
        for p in pats:
            if re.search(p, text):
                return p
        return None

    # ================================================================== startup
    def _open_viewer(self, doc: Path) -> None:
        self.adapter = self.adapter_factory(self.checkpoint, self.estop)
        self.prompts = PromptHandler(self.cfg.prompts, self.adapter, self.checkpoint, self.estop,
                                     record=lambda r: self._record_event("prompts_handled", r))
        if not self.cfg.viewer.attach_only:
            self.adapter.launch(doc)
        self.adapter.attach(doc, self.cfg.viewer.launch_timeout_s)
        self.source = self.source_factory(self.adapter)
        if self.adapter.capture_protected():
            raise CaptureBlocked(
                "The reader window requests exclusion from screen capture (display affinity). "
                "The harness does not bypass this; use a document/reader configuration that permits capture.")
        self.navigator = Navigator(self.cfg, self.adapter, self._observe_settled, self.checkpoint)

    def _wait_until_loaded(self) -> None:
        t0 = time.monotonic()
        deadline = t0 + self.cfg.prompts.startup_timeout_s
        while True:
            self.estop.check()
            try:
                self.adapter.bring_to_foreground()
            except FocusLost:
                pass
            self.prompts.handle(self._safe_doc_rect())
            remaining = max(1.0, deadline - time.monotonic())
            res = self._stable(timeout=min(remaining, self.cfg.capture.stability.timeout_s))
            if self.prompts.handle(self._safe_doc_rect()):
                continue
            ok = res.stable and res.frame is not None and not is_all_black(res.frame)
            ph = self._placeholder(res.frame) if ok else None
            if ok and not ph:
                event(self.log, "document_loaded", f"view stable after {time.monotonic() - t0:.1f}s",
                      frames=res.frames, busy_seen=res.busy_seen)
                return
            if time.monotonic() >= deadline:
                why = (f"placeholder text matched {ph!r}" if ph else
                       "capture is black" if res.frame is not None and is_all_black(res.frame) else
                       "the view never became stable")
                ans = self.checkpoint("startup", f"The document did not finish loading ({why}). "
                                                 "Bring it to a fully rendered state, then choose Retry.")
                deadline = time.monotonic() + self.cfg.prompts.startup_timeout_s
                if ans == "continue":
                    return
            else:
                self._sleep(self.cfg.capture.placeholder_wait_s if ph else 0.5)

    def _with_focus(self, fn):
        """Run ``fn``; if the reader lost focus, ask the operator and retry once."""
        try:
            return fn()
        except FocusLost as exc:
            self.checkpoint("focus_lost", f"{exc}. Click the reader window (without changing the page), "
                                          "then choose Retry.")
            return fn()

    def _check_layout(self) -> None:
        """Before page 1: nothing drawn over the page, and exactly one page in view (fit_page mode)."""
        if not (self.cfg.capture.layout_check and self.cfg.capture.mode == "fit_page"):
            return
        tol = self.cfg.capture.crop.canvas_tolerance
        reapplied = overlays_accepted = False
        while True:
            if not overlays_accepted:
                overlays = self.adapter.find_overlays(self._safe_doc_rect())
                if overlays:
                    event(self.log, "overlay_on_page", ", ".join(overlays), level=30)
                    ans = self.checkpoint(
                        "overlay", f"The reader shows {', '.join(repr(o) for o in overlays)} on top of the page, "
                        "which would hide part of every captured page. Turn it off in the reader (Acrobat AI "
                        "Assistant: Menu > Preferences > Generative AI > untick 'Enable generative AI features', "
                        "then restart Acrobat), then choose Retry.")
                    if ans == "continue":
                        overlays_accepted = True
                    continue
            res = self._stable()
            diag = layout_diagnostics(res.frame, tol)
            self._set_page_region(diag["page_box"], res.frame.size)
            event(self.log, "layout", f"page detected at {diag['page_box']} in a {diag['viewport']} view "
                  f"(canvas {diag['canvas_rgb']}, {diag['mode']})", level=20)
            if diag["page_box"] is None:
                event(self.log, "layout_unknown", "could not locate the page in the view; pages will be "
                      "kept uncropped and flagged for review", level=30)
            issues = layout_issues(res.frame, tol)
            if not issues:
                return
            event(self.log, "layout_problem", ", ".join(issues), level=30, **diag)
            if not reapplied and self.cfg.viewer.prepare_keys:
                # The fit-page key may not have reached the page view; press it again once.
                reapplied = True
                event(self.log, "layout_retry", "re-applying the fit-page zoom and checking again")
                self._with_focus(self.adapter.prepare_view)
                continue
            ans = self.checkpoint("layout", "More than one page is visible in the reader (continuous or two-page "
                                  "layout). Set Single Page view (Acrobat: Menu > Preferences > Page Display > "
                                  "Page Layout = Single Page), then choose Retry. Ignore continues anyway (pages "
                                  "will be flagged).")
            if ans == "continue":
                return

    def _safe_doc_rect(self) -> Optional[Rect]:
        try:
            return self.adapter.document_rect()
        except Exception:
            return None

    # ================================================================== capture
    def _capture_page(self, page_no: int) -> PageCapture:
        c = self.cfg.capture
        retry_reasons: list[str] = []
        last_img, last_raw, flags, info = None, None, [], {}
        ind = PageIndicator()
        attempts = 0
        for attempt in range(1, c.retries + 2):
            attempts = attempt
            self.estop.check()
            flags = []
            if self._dialogs(self._safe_doc_rect()):
                self._sleep(0.3)
            rect = self._doc_rect()
            self._check_rect_on_screen(rect)
            t0 = time.monotonic()
            res = self._stable(rect)
            info = {"stability_s": round(res.elapsed_s, 3), "frames": res.frames,
                    "busy_seen": res.busy_seen, "document_rect": rect.to_dict()}
            if self._dialogs(rect):              # a dialog popped up during capture
                retry_reasons.append("dialog_during_capture")
                continue
            img = res.frame
            last_raw = img
            ind = self._indicator()
            if not res.stable:
                flags.append(Flag.UNSTABLE)
                retry_reasons.append(Flag.UNSTABLE)
                last_img = img
                continue
            if is_all_black(img):
                flags.append(Flag.ALL_BLACK)
                retry_reasons.append(Flag.ALL_BLACK)
                last_img = img
                self.save_diagnostics(f"page{page_no:04d}_black")
                self._sleep(1.0)
                continue
            ph = self._placeholder(img)
            if ph:
                flags.append(Flag.PLACEHOLDER)
                retry_reasons.append(f"{Flag.PLACEHOLDER}:{ph}")
                last_img = img
                self._sleep(c.placeholder_wait_s)
                continue
            if c.mode == "scroll":
                img, sflags, sinfo, last_raw = self._scroll_capture(page_no, rect, img, ind)
                flags += sflags
                info.update(sinfo)
            last_img = img
            info["capture_s"] = round(time.monotonic() - t0, 3)
            break
        else:
            if last_img is None:
                flags.append(Flag.CAPTURE_FAILED)
            self.save_diagnostics(f"page{page_no:04d}_retries_exhausted")
        if last_img is not None and is_uniform(last_img, c.blank_std_threshold):
            flags.append(Flag.BLANK)
        return PageCapture(last_img, last_raw, ind, flags, attempts, retry_reasons, info)

    def _scroll_capture(self, page_no: int, rect: Rect, first: Image.Image, ind0: PageIndicator):
        sc = self.cfg.capture.scroll
        slices = [first]
        flags: list[str] = []
        crossed = False
        last_view = first
        for _ in range(sc.max_slices - 1):
            self.adapter.scroll_down(sc.wheel_notches)
            res = self._stable(rect)
            last_view = res.frame
            ind = self._indicator()
            if ind0.readable and ind.readable and ind.current != ind0.current:
                crossed = True
                break
            if changed_fraction(slices[-1], res.frame, self.cfg.capture.stability.pixel_threshold) \
                    <= self.cfg.capture.same_page_threshold:
                break   # bottom of page reached
            slices.append(res.frame)
        else:
            flags.append(Flag.SLICES_TRUNCATED)
        if self.cfg.output.save_raw_slices:
            d = self.paths.slices / f"page_{page_no:04d}"
            d.mkdir(parents=True, exist_ok=True)
            for i, s in enumerate(slices):
                s.save(d / f"slice_{i:03d}.png")
        stitched, matches = stitch_vertical(slices, sc.template_rows, sc.max_mean_diff, sc.min_template_std)
        if any((not m.accepted) or m.ambiguous for m in matches):
            flags.append(Flag.STITCH_UNCERTAIN)
        if crossed:
            flags.append(Flag.BOUNDARY_CROSSED)
            # Return to the top of this page's successor is handled by advance();
            # step back so the viewer is on this page again.
            self.adapter.prev_page()
            last_view = self._stable(rect).frame
        # `last_view` is what the viewer shows now; navigation compares against it.
        return stitched, flags, {"slices": len(slices),
                                 "stitch": [{"offset": m.offset, "diff": round(m.mean_diff, 2),
                                             "ambiguous": m.ambiguous, "accepted": m.accepted} for m in matches]}, last_view

    def _save_page(self, page_no: int, cap: PageCapture, nav_flags: list[str], timings: dict) -> dict:
        flags = list(dict.fromkeys(nav_flags + cap.flags))
        rec = {"page": page_no, "attempts": cap.attempts, "retries": cap.attempts - 1,
               "retry_reasons": cap.retry_reasons, "indicator": {"current": cap.indicator.current,
               "total": cap.indicator.total, "raw": cap.indicator.raw, "source": cap.indicator.source},
               "timings": timings, **cap.info, "captured_at": now_iso()}
        if cap.image is None:
            rec.update(status="failed", flags=flags)
            self.manifest.set_page(page_no, rec)
            return rec
        img = cap.image
        if self.cfg.capture.layout_check and self.cfg.capture.mode == "fit_page":
            flags += layout_issues(img, self.cfg.capture.crop.canvas_tolerance)
        if self.cfg.capture.crop.auto_page:
            cc = self.cfg.capture.crop
            cr = crop_to_page(img, cc.canvas_tolerance, cc.padding, cc.min_page_fraction)
            img = cr.image
            rec["crop_box"] = list(cr.box)
            if not cr.fallback and self.cfg.capture.mode == "fit_page":
                self._set_page_region(cr.box, cap.image.size)
            rec["canvas_color"] = cr.canvas_color
            if cr.fallback:
                flags.append(Flag.CROP_FALLBACK)
        if cap.indicator.readable and cap.indicator.current != page_no:
            flags.append(Flag.INDICATOR_MISMATCH)
        h = dhash(img)
        dups = [p for p, ph in self._hashes.items()
                if p != page_no and p != page_no - 1 and hamming(ph, h) <= self.cfg.capture.duplicate_hash_distance]
        if dups:
            flags.append(Flag.DUPLICATE)
            rec["duplicate_of"] = dups
        self._hashes[page_no] = h
        path = self.paths.page_png(page_no)
        img.save(path, compress_level=self.cfg.output.png_compress_level)
        rec.update(status="captured", image=str(path.relative_to(self.paths.root)).replace("\\", "/"),
                   size=list(img.size), sha256=sha256_image(img), dhash=f"{h:064x}",
                   flags=list(dict.fromkeys(flags)))
        self.manifest.set_page(page_no, rec)
        event(self.log, "page_captured", f"page {page_no} captured", level=10, page=page_no,
              attempts=cap.attempts, flags=rec["flags"], size=rec["size"])
        return rec

    # ================================================================== main loop
    def _known_total(self, ind: PageIndicator) -> tuple[Optional[int], str]:
        known = self.cfg.document.known_page_count
        if known is not None:
            if ind.total is not None and ind.total != known:
                event(self.log, "page_count_disagreement",
                      f"known_page_count={known} but viewer reports {ind.total}", level=30)
                self.manifest.data["page_count_disagreement"] = {"known": known, "viewer": ind.total}
            return known, "known_page_count"
        if ind.total is not None and self.navigator.indicator_trusted:
            return ind.total, "viewer_indicator"
        if self._page_count_hint:
            return self._page_count_hint, "viewer_page_count"
        return None, "unknown"

    def _end(self, reason: EndReason, detail: str, last_page: int) -> None:
        self.manifest.data["end"] = {"reason": reason.value, "detail": detail, "last_page": last_page,
                                     "ts": now_iso()}
        self.manifest.save()
        event(self.log, "end_of_document", f"{reason.value}: {detail}", last_page=last_page)

    def _capture_loop(self) -> None:
        start = self.manifest.next_page_to_capture(self.cfg.document.start_page)
        for n in self.manifest.captured_pages():          # duplicate detection across sessions
            dh = self.manifest.page(n).get("dhash")
            if dh:
                self._hashes[n] = int(dh, 16)
        max_pages = self.cfg.document.max_pages
        t_nav = time.monotonic()
        last_rec = self.manifest.page(start - 1) if start > 1 else None
        if last_rec and last_rec.get("status") == "captured":
            # Resume: return to the last captured page (recognised by its saved image when the
            # page number is not readable) and move on from there exactly like a normal run, so
            # a run that stopped at the last page simply finishes.
            last = start - 1
            event(self.log, "resume_position", f"resuming: returning to page {last} (last captured), "
                  "then continuing", page=start)
            ok, state = self._with_focus(lambda: self.navigator.goto(last, verify=lambda st: self._matches_saved(last, st)))
            if not ok:
                self.checkpoint("goto_start", f"Could not verify the reader is on page {last}. "
                                              f"Navigate to page {last} and choose Retry.")
                state = self._observe()
            nav_flags = self._advance_from(last, state)
            if nav_flags is None:
                return
        else:
            if start > 1:
                event(self.log, "resume_position", f"resuming at page {start}", page=start)
            ok, state = self._with_focus(lambda: self.navigator.goto(start))
            if not ok:
                self.checkpoint("goto_start", f"Could not verify the reader is on page {start}. "
                                              f"Navigate to page {start} and choose Retry.")
                state = self._observe()
            nav_flags = []
        page = start
        nav_s = time.monotonic() - t_nav
        t_loop, pages_done = time.monotonic(), 0
        t_prev, timers_prev = time.monotonic(), dict(self._timers)
        while True:
            self.estop.check()
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise UserAbort(f"document time limit of {self.cfg.document.time_limit_minutes} min reached "
                                f"before page {page} (resume to continue)")
            total, total_src = self._known_total(state.indicator)
            self.manifest.data["expected_pages"] = {"count": total, "source": total_src}
            if page > max_pages:
                self._end(EndReason.MAX_PAGES, f"max_pages={max_pages}", page - 1)
                return
            t0 = time.monotonic()
            cap = self._capture_page(page)
            cap_s = time.monotonic() - t0
            self._save_page(page, cap, nav_flags, {"navigation_s": round(nav_s, 3), "capture_s": round(cap_s, 3)})
            if self.pipeline is not None:
                self.pipeline.submit(page)
                self.pipeline.poll()
            pages_done += 1
            now = time.monotonic()
            rate = (now - t_loop) / pages_done
            cycle = now - t_prev
            part = {k: self._timers[k] - timers_prev.get(k, 0.0)
                    for k in ("render_wait", "dialog_checks", "page_number")}
            other = max(0.0, cycle - sum(part.values()))
            t_prev, timers_prev = now, dict(self._timers)
            tot = total or None
            eta = f", ETA {((tot - page) * rate) / 60:.1f} min" if tot and tot > page else ""
            event(self.log, "progress", f"page {page}/{tot or '?'} captured in {cycle:.1f}s "
                  f"[render wait {part['render_wait']:.1f}, dialog checks {part['dialog_checks']:.1f}, "
                  f"page number {part['page_number']:.1f}, keys/other {other:.1f}] "
                  f"avg {rate:.1f}s/page{eta}; OCR done {self.pipeline.done if self.pipeline else 0}, "
                  f"queued {len(self.pipeline.pending) if self.pipeline else 0}", page=page,
                  seconds=round(cycle, 2), **{f"{k}_s": round(v, 2) for k, v in part.items()})
            state = ViewState(cap.raw_frame if cap.raw_frame is not None else state.frame,
                              cap.indicator if cap.indicator.readable else state.indicator, True)
            t_nav = time.monotonic()
            nav_flags = self._advance_from(page, state)
            if nav_flags is None:
                return
            nav_s = time.monotonic() - t_nav
            page += 1

    def _matches_saved(self, n: int, st: ViewState) -> bool:
        """Does the current view show the page saved as page ``n`` (by image hash)?"""
        rec = self.manifest.page(n) or {}
        if not rec.get("dhash") or st.frame is None:
            return False
        cc = self.cfg.capture.crop
        img = crop_to_page(st.frame, cc.canvas_tolerance, cc.padding, cc.min_page_fraction).image \
            if cc.auto_page else st.frame
        dist = hamming(dhash(img), int(rec["dhash"], 16))
        event(self.log, "resume_verify", f"view vs saved page {n}: hash distance {dist}", level=10)
        return dist <= 12

    def _advance_from(self, page: int, state: ViewState) -> Optional[list[str]]:
        """Move from ``page`` to ``page + 1``. Returns the navigation flags for the
        next page, or None when the document has ended (the end is recorded)."""
        max_pages = self.cfg.document.max_pages
        total, total_src = self._known_total(state.indicator)
        if total is not None and page >= total:
            self._end(EndReason.INDICATOR_TOTAL if total_src == "viewer_indicator" else EndReason.KNOWN_PAGE_COUNT,
                      f"{total_src}={total}", page)
            return None
        if page >= max_pages:
            self._end(EndReason.MAX_PAGES, f"max_pages={max_pages}", page)
            return None
        outcome = self.navigator.advance(page, state, total if total_src in ("known_page_count", "viewer_page_count") else None)
        nav_flags = list(outcome.flags)
        while outcome.status == NavStatus.FAILED:
            self.save_diagnostics(f"page{page:04d}_nav_failed")
            self._record_event("errors", {"type": "navigation_failed", "page": page, "detail": outcome.reason})
            ans = self.checkpoint("navigation_failed",
                                  f"Could not advance from page {page} to {page + 1}: {outcome.reason}.\n"
                                  f"Navigate the reader to page {page + 1} yourself, then choose Retry to "
                                  "verify it, Ignore to accept the current view as that page, or Abort.")
            if ans == "skip":
                self._end(EndReason.NAVIGATION_FAILED, outcome.reason, page)
                return None
            cur = self._observe()
            ind = self.navigator._ind(cur.indicator)
            if ans == "continue":
                nav_flags = [Flag.MANUAL_OVERRIDE]
                if ind.readable and ind.current != page + 1:
                    self.navigator.indicator_trusted = False
                    event(self.log, "indicator_untrusted", "operator override contradicts indicator", level=30)
                break
            prev_ind = self.navigator._ind(state.indicator)
            if (ind.readable and prev_ind.readable and ind.current == page + 1) or \
                    (not ind.readable and self.navigator._differs(state.frame, cur.frame)):
                nav_flags = [Flag.MANUAL_OVERRIDE]
                break
            outcome = self.navigator.advance(page, state, total if total_src in ("known_page_count", "viewer_page_count") else None)
            nav_flags = list(outcome.flags)
        if outcome.status == NavStatus.END:
            self._end(outcome.end_reason or EndReason.STALL_UNCONFIRMED, outcome.reason, page)
            return None
        return nav_flags

    # ================================================================== OCR
    def _orig(self):
        """Page sizes/metadata of the original PDF (read once, never decrypted)."""
        if self._orig_info is None:
            from .pdf_output import OriginalInfo
            self._orig_info = OriginalInfo(Path(self.manifest.data["document"]["path"]))
        return self._orig_info

    def _ensure_pipeline(self) -> OcrPipeline:
        if self.pipeline is None:
            from .pdf_output import make_width_fn
            width_fn = make_width_fn(self.cfg, self._orig()) if not self.skip_pdf else None
            self.pipeline = OcrPipeline(self.cfg, self.paths, self.manifest, width_fn)
            event(self.log, "ocr_pipeline", f"{self.pipeline.workers} parallel OCR worker(s)")
        return self.pipeline

    def run_ocr(self, redo: bool = False) -> dict:
        from .ocr import resolve_tesseract

        cmd = resolve_tesseract(self.cfg.ocr)
        t0 = time.monotonic()
        m = self.manifest
        pipe = self._ensure_pipeline()
        for n in m.captured_pages():
            rec = m.page(n)
            if n in pipe.pending:
                continue
            if redo or not rec.get("ocr") or not self.paths.page_txt(n).exists():
                pipe.submit(n)
        remaining = len(pipe.pending)
        event(self.log, "ocr_start", f"OCR: {remaining} page(s) still to process "
              f"({pipe.done} done during capture), {pipe.workers} worker(s)", tesseract=cmd,
              lang=self.cfg.ocr.lang, psm=self.cfg.ocr.psm)
        last_report = [time.monotonic()]

        def progress(done, total):
            if time.monotonic() - last_report[0] > 10:
                last_report[0] = time.monotonic()
                event(self.log, "ocr_progress", f"OCR {done}/{total} remaining pages done")

        pipe.drain(self.estop.check, progress)
        self._write_combined()
        total = round(time.monotonic() - t0, 3)
        m.data["timings"]["ocr_after_capture_s"] = total
        m.data["timings"]["ocr_cpu_s"] = round(sum((m.page(n).get("ocr") or {}).get("seconds", 0)
                                                    for n in m.captured_pages()), 3)
        m.data["ocr_engine"] = {"tesseract_cmd": cmd, "lang": self.cfg.ocr.lang, "psm": self.cfg.ocr.psm,
                                "oem": self.cfg.ocr.oem, "workers": pipe.workers}
        if pipe.failed:
            m.data["ocr_engine"]["failed_pages"] = sorted(pipe.failed)
        try:
            import pytesseract
            m.data["ocr_engine"]["tesseract_version"] = str(pytesseract.get_tesseract_version())
        except Exception:
            pass
        m.save()
        return {"seconds": total}

    def _write_combined(self) -> None:
        from .state import atomic_write_text
        m = self.manifest
        nums = m.page_numbers()
        if not nums:
            atomic_write_text(self.paths.ocr / "combined.txt", "")
            return
        parts = []
        for n in range(1, max(nums) + 1):
            rec = m.page(n)
            if rec is None or rec.get("status") != "captured":
                status = "not captured" if rec is None else rec.get("status", "unknown")
                parts.append(f"===== Page {n} (MISSING: {status}) =====\n")
                continue
            txt = self.paths.page_txt(n)
            body = txt.read_text(encoding="utf-8") if txt.exists() else "(no OCR output)\n"
            parts.append(f"===== Page {n} =====\n{body}")
        text = "\n".join(parts)
        atomic_write_text(self.paths.ocr / "combined.txt", text)
        if self.cfg.ocr.combined_copy_path:
            atomic_write_text(Path(self.cfg.ocr.combined_copy_path), text)

    # ================================================================== entry points
    def run(self) -> int:
        from .report import write_report

        t0 = time.monotonic()
        self.estop.start()
        code = EXIT_FAILED
        try:
            doc = self._prepare_document()
            event(self.log, "run_start", f"run dir {self.paths.root}", document=self.cfg.document.path,
                  viewer=self.cfg.viewer.profile, resume=self.resume)
            ocr_on = self.cfg.ocr.enabled and not self.skip_ocr
            if ocr_on:
                from .ocr import resolve_tesseract
                try:
                    resolve_tesseract(self.cfg.ocr)
                except FileNotFoundError as exc:
                    raise HarnessError(f"{exc} Run `python -m doc_capture_harness doctor` for setup help.")
            if self.cfg.document.time_limit_minutes:
                self.deadline = time.monotonic() + self.cfg.document.time_limit_minutes * 60
            if self.manifest.data.get("status_capture") == "complete" and self.resume:
                event(self.log, "capture_already_complete", "capture finished earlier; running OCR/report only")
            else:
                t_open = time.monotonic()
                self._open_viewer(doc)
                self._wait_until_loaded()
                if self.cfg.viewer.setup_checkpoint:
                    self.checkpoint("setup", self.cfg.viewer.setup_checkpoint)
                self._with_focus(self.adapter.prepare_view)
                self._wait_until_loaded()
                self._check_layout()
                if self.cfg.document.known_page_count is None:
                    self._page_count_hint = self._with_focus(self.adapter.page_count_hint)
                    if self._page_count_hint:
                        event(self.log, "page_count", f"the reader reports {self._page_count_hint} page(s); "
                              "capture stops after the last one")
                env = self.adapter.environment_info()
                env["dpi_awareness_requested"] = os.environ.get("DOC_CAPTURE_DPI_MODE", "")
                self.manifest.data["environment"] = env
                self.manifest.data["timings"]["open_s"] = round(time.monotonic() - t_open, 3)
                self.manifest.save()
                if ocr_on:
                    self._ensure_pipeline()
                t_cap = time.monotonic()
                self._capture_loop()
                self.manifest.data["timings"]["capture_s"] = round(
                    self.manifest.data["timings"].get("capture_s", 0) + time.monotonic() - t_cap, 3)
                self.manifest.data["timings"]["capture_breakdown_s"] = {
                    k: round(v, 2) for k, v in self._timers.items()}
                self.manifest.data["status_capture"] = "complete"
                self.manifest.save()
                if self.cfg.viewer.close_on_finish:
                    self.adapter.close()
                    self.prompts.handle(None)
            if ocr_on:
                self.run_ocr()
            if self.cfg.pdf.enabled and not self.skip_pdf:
                self.make_pdf()
            self.manifest.data["status"] = "completed"
            code = EXIT_OK
        except (EmergencyStop, KeyboardInterrupt) as exc:
            self._fail("stopped", EndReason.EMERGENCY_STOP, str(exc) or "Ctrl+C")
            code = EXIT_STOPPED
        except UserAbort as exc:
            self._fail("aborted", EndReason.USER_ABORT, str(exc))
            code = EXIT_STOPPED
        except Exception as exc:  # noqa: BLE001 - top-level guard
            event(self.log, "run_error", f"{type(exc).__name__}: {exc}", level=40,
                  traceback=traceback.format_exc())
            self._fail("failed", EndReason.ERROR, f"{type(exc).__name__}: {exc}")
            code = EXIT_FAILED
        finally:
            self.estop.stop()
            if self.pipeline is not None:
                self.pipeline.close(cancel=True)
            if self.manifest is not None:
                self.manifest.data["timings"]["last_session_s"] = round(time.monotonic() - t0, 3)
                self.manifest.data["document_integrity"] = self._verify_original()
                self.manifest.data["input_fallback_uses"] = getattr(
                    getattr(self.adapter, "inputs", None), "fallback_uses", 0)
                self.manifest.finalize()
                summary = write_report(self.manifest, self.paths)
                pdf_bad = bool(summary.get("pdf")) and not summary["pdf"]["verification"]["ok"]
                if code == EXIT_OK and (summary["pages_needing_review"] or summary["end_is_uncertain"]
                                        or not summary["document_unchanged"] or pdf_bad):
                    code = EXIT_WARNINGS
                event(self.log, "run_finished", f"exit code {code}", status=self.manifest.data["status"],
                      report=str(self.paths.report_md))
            if self.source is not None:
                self.source.close()
        return code

    def _fail(self, status: str, reason: EndReason, detail: str) -> None:
        if self.manifest is None:
            return
        self.save_diagnostics(status)
        self.manifest.data["status"] = status
        if not self.manifest.data.get("end"):
            last = max(self.manifest.captured_pages(), default=0)
            self.manifest.data["end"] = {"reason": reason.value, "detail": detail, "last_page": last,
                                         "ts": now_iso()}
        self.manifest.add_event("errors", {"type": status, "detail": detail})
        event(self.log, f"run_{status}", detail, level=40)

    def make_pdf(self, **kwargs) -> dict:
        from .pdf_output import build_pdf
        kwargs.setdefault("orig", self._orig())
        return build_pdf(self.cfg, self.manifest, self.paths, estop=self.estop, **kwargs)

    def pdf_only(self, **kwargs) -> int:
        from .report import write_report
        self.manifest = Manifest.load(self.paths)
        res = self.make_pdf(**kwargs)
        self.manifest.finalize()
        summary = write_report(self.manifest, self.paths)
        ok = res["verification"]["ok"] and not summary["pages_needing_review"]
        return EXIT_OK if ok else EXIT_WARNINGS

    def ocr_only(self, redo: bool) -> int:
        from .report import write_report
        self.manifest = Manifest.load(self.paths)
        try:
            self.run_ocr(redo=redo)
        finally:
            if self.pipeline is not None:
                self.pipeline.close(cancel=True)
        self.manifest.finalize()
        summary = write_report(self.manifest, self.paths)
        return EXIT_WARNINGS if summary["pages_needing_review"] else EXIT_OK
