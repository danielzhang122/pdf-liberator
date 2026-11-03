"""Orchestration: open -> wait for load -> capture pages -> OCR -> report.

The runner is reader-agnostic: it talks to a :class:`ViewerAdapter` and a
:class:`CaptureSource`. On Windows the CLI wires in the pywinauto adapters and
``mss``; the test-suite wires in a simulated viewer.
"""
from __future__ import annotations

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
                      sha256_image, stitch_vertical)
from .logging_utils import event, setup_logging
from .manual import ManualCheckpoint
from .models import EndReason, Flag, NavStatus, PageIndicator, Rect
from .navigation import Navigator, ViewState
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
                 skip_pdf: bool = False):
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
        self.estop = EmergencyStopMonitor(cfg.safety.estop_hotkey, self.paths.root / cfg.safety.stop_file_name)
        self.manifest: Optional[Manifest] = None
        self.checkpoint = ManualCheckpoint(cfg.manual, self.estop, diagnostics=self.save_diagnostics,
                                           record=lambda r: self._record_event("manual_checkpoints", r),
                                           responder=responder)
        self.adapter: Optional[ViewerAdapter] = None
        self.source: Optional[CaptureSource] = None
        self.prompts: Optional[PromptHandler] = None
        self.navigator: Optional[Navigator] = None
        self._hashes: dict[int, int] = {}
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
                    ind = self.adapter.read_page_indicator()
                    info["indicator"] = {"current": ind.current, "total": ind.total, "raw": ind.raw}
                except Exception as exc:
                    info["indicator_error"] = str(exc)
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
        try:
            from . import platform_win as pw
        except Exception:
            return
        vs = pw.virtual_screen()
        if r.intersect(vs) != r:
            self.checkpoint("offscreen", f"The document area {r.to_dict()} is partly outside the desktop "
                                         f"{vs.to_dict()}. Move/maximize the reader window, then Retry.")
        mons = pw.monitors_for_rect(r)
        scales = {m.scale for m in mons}
        if len(mons) > 1:
            event(self.log, "multi_monitor_span", "document area spans several monitors", level=30,
                  monitors=[m.device for m in mons], scales=sorted(s for s in scales if s))
            if len(scales) > 1:
                self.checkpoint("mixed_dpi", "The reader window spans monitors with different scaling. "
                                             "Move it fully onto one monitor, then Retry.")

    def _stable(self, rect: Optional[Rect] = None, timeout: Optional[float] = None):
        rect = rect or self._doc_rect()
        self.adapter.park_cursor()
        return wait_for_stable(lambda: self.source.grab(rect), self.cfg.capture.stability,
                               sleep=self._sleep, is_busy=self.adapter.is_busy, timeout_s=timeout)

    def _observe(self) -> ViewState:
        res = self._stable()
        ind = self.adapter.read_page_indicator()
        return ViewState(res.frame, ind, res.stable)

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
        self.navigator = Navigator(self.cfg, self.adapter, self._observe, self.checkpoint)

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
            if self.prompts.handle(self._safe_doc_rect()):
                self._sleep(0.3)
            rect = self._doc_rect()
            self._check_rect_on_screen(rect)
            t0 = time.monotonic()
            res = self._stable(rect)
            info = {"stability_s": round(res.elapsed_s, 3), "frames": res.frames,
                    "busy_seen": res.busy_seen, "document_rect": rect.to_dict()}
            if self.prompts.handle(rect):        # a dialog popped up during capture
                retry_reasons.append("dialog_during_capture")
                continue
            img = res.frame
            last_raw = img
            ind = self.adapter.read_page_indicator()
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
            ind = self.adapter.read_page_indicator()
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
        if self.cfg.capture.crop.auto_page:
            cc = self.cfg.capture.crop
            cr = crop_to_page(img, cc.canvas_tolerance, cc.padding, cc.min_page_fraction)
            img = cr.image
            rec["crop_box"] = list(cr.box)
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
        event(self.log, "page_captured", f"page {page_no} captured", page=page_no, attempts=cap.attempts,
              flags=rec["flags"], size=rec["size"])
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
        if start > 1:
            event(self.log, "resume_position", f"resuming at page {start}", page=start)
        max_pages = self.cfg.document.max_pages
        t_nav = time.monotonic()
        ok, state = self._with_focus(lambda: self.navigator.goto(start))
        if not ok:
            self.checkpoint("goto_start", f"Could not verify the reader is on page {start}. "
                                                f"Navigate to page {start} and choose Retry.")
            state = self._observe()
        nav_flags: list[str] = []
        page = start
        nav_s = time.monotonic() - t_nav
        while True:
            self.estop.check()
            total, total_src = self._known_total(state.indicator)
            self.manifest.data["expected_pages"] = {"count": total, "source": total_src}
            if page > max_pages:
                self._end(EndReason.MAX_PAGES, f"max_pages={max_pages}", page - 1)
                return
            t0 = time.monotonic()
            cap = self._capture_page(page)
            cap_s = time.monotonic() - t0
            self._save_page(page, cap, nav_flags, {"navigation_s": round(nav_s, 3), "capture_s": round(cap_s, 3)})
            state = ViewState(cap.raw_frame if cap.raw_frame is not None else state.frame,
                              cap.indicator if cap.indicator.readable else state.indicator, True)
            total, total_src = self._known_total(state.indicator)
            if total is not None and page >= total:
                self._end(EndReason.KNOWN_PAGE_COUNT if total_src == "known_page_count" else EndReason.INDICATOR_TOTAL,
                          f"{total_src}={total}", page)
                return
            if page >= max_pages:
                self._end(EndReason.MAX_PAGES, f"max_pages={max_pages}", page)
                return
            t_nav = time.monotonic()
            outcome = self.navigator.advance(page, state, total if total_src == "known_page_count" else None)
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
                    return
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
                outcome = self.navigator.advance(page, state, total if total_src == "known_page_count" else None)
                nav_flags = list(outcome.flags)
            if outcome.status == NavStatus.END:
                self._end(outcome.end_reason or EndReason.STALL_UNCONFIRMED, outcome.reason, page)
                return
            nav_s = time.monotonic() - t_nav
            page += 1

    # ================================================================== OCR
    def run_ocr(self, redo: bool = False) -> dict:
        from .ocr import ocr_image, resolve_tesseract

        cmd = resolve_tesseract(self.cfg.ocr)
        event(self.log, "ocr_start", f"tesseract: {cmd}", lang=self.cfg.ocr.lang, psm=self.cfg.ocr.psm)
        t0 = time.monotonic()
        m = self.manifest
        for n in m.captured_pages():
            self.estop.check()
            rec = m.page(n)
            if rec.get("ocr") and not redo and self.paths.page_txt(n).exists():
                continue
            ts = time.monotonic()
            img = Image.open(self.paths.root / rec["image"])
            res = ocr_image(img, self.cfg.ocr)
            from .state import atomic_write_text
            atomic_write_text(self.paths.page_txt(n), res.text)
            if self.cfg.ocr.save_tsv:
                atomic_write_text(self.paths.page_tsv(n), res.tsv)
            rec["ocr"] = res.summary() | {"seconds": round(time.monotonic() - ts, 3),
                                          "text_file": f"ocr/page_{n:04d}.txt"}
            m.set_page(n, rec)
            event(self.log, "page_ocr", f"page {n}: {res.words} words, mean conf {res.mean_conf}",
                  page=n, flags=res.flags)
        self._write_combined()
        total = round(time.monotonic() - t0, 3)
        m.data["timings"]["ocr_s"] = total
        m.data["ocr_engine"] = {"tesseract_cmd": cmd, "lang": self.cfg.ocr.lang, "psm": self.cfg.ocr.psm,
                                "oem": self.cfg.ocr.oem}
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
        atomic_write_text(self.paths.ocr / "combined.txt", "\n".join(parts))

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
                env = self.adapter.environment_info()
                env["dpi_awareness_requested"] = os.environ.get("DOC_CAPTURE_DPI_MODE", "")
                self.manifest.data["environment"] = env
                self.manifest.data["timings"]["open_s"] = round(time.monotonic() - t_open, 3)
                self.manifest.save()
                t_cap = time.monotonic()
                self._capture_loop()
                self.manifest.data["timings"]["capture_s"] = round(
                    self.manifest.data["timings"].get("capture_s", 0) + time.monotonic() - t_cap, 3)
                self.manifest.data["status_capture"] = "complete"
                self.manifest.save()
                if self.cfg.viewer.close_on_finish:
                    self.adapter.close()
                    self.prompts.handle(None)
            if self.cfg.ocr.enabled and not self.skip_ocr:
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
            if self.manifest is not None:
                self.manifest.data["timings"]["last_session_s"] = round(time.monotonic() - t0, 3)
                self.manifest.data["document_integrity"] = self._verify_original()
                self.manifest.data["input_fallback_uses"] = getattr(
                    getattr(self.adapter, "inputs", None), "fallback_uses", 0)
                self.manifest.save()
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
        return build_pdf(self.cfg, self.manifest, self.paths, estop=self.estop, **kwargs)

    def pdf_only(self, **kwargs) -> int:
        from .report import write_report
        self.manifest = Manifest.load(self.paths)
        res = self.make_pdf(**kwargs)
        summary = write_report(self.manifest, self.paths)
        ok = res["verification"]["ok"] and not summary["pages_needing_review"]
        return EXIT_OK if ok else EXIT_WARNINGS

    def ocr_only(self, redo: bool) -> int:
        from .report import write_report
        self.manifest = Manifest.load(self.paths)
        self.run_ocr(redo=redo)
        summary = write_report(self.manifest, self.paths)
        return EXIT_WARNINGS if summary["pages_needing_review"] else EXIT_OK
