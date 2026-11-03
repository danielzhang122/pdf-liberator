"""Parallel OCR that runs alongside capture.

Capture is inherently sequential (one reader on one desktop) and mostly waits
on rendering, while OCR is CPU-bound. Each captured page is therefore handed
to a worker pool straight away, so by the time the last page is captured
most of the OCR is done.

Workers are threads that each run a Tesseract *process*, so CPU work is truly
parallel and nothing has to be pickled (safe with Windows' spawn model).
Tesseract's own OpenMP threading is limited to one thread per process when
several workers run, which is faster overall than oversubscribing the CPU.

Each job does a single Tesseract pass producing the text, the word TSV and the
page's searchable PDF. Results are recorded into the manifest only from the
main thread (:meth:`OcrPipeline.poll`), so manifest writes never race.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Callable, Optional

from PIL import Image

from .config import HarnessConfig
from .logging_utils import event, get_logger
from .state import Manifest, RunPaths, atomic_write_text

# (page_no, image width px, image height px) -> PDF page width in points (None = no PDF)
WidthFn = Callable[[int, int, int], Optional[float]]


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def _job(n: int, png: str, cfg: HarnessConfig, paths: RunPaths, width_pt: Optional[float]) -> dict:
    from .ocr import ocr_image
    from .pdf_output import scale_single_page

    t0 = time.monotonic()
    with Image.open(png) as im:
        img = im.convert("RGB")
    pdf_opts = None
    if width_pt:
        pdf_opts = {"dpi": round(img.width * 72.0 / width_pt), "image_format": cfg.pdf.image_format,
                    "jpeg_quality": cfg.pdf.jpeg_quality}
    res, pdf = ocr_image(img, cfg.ocr, pdf_opts=pdf_opts)
    atomic_write_text(paths.page_txt(n), res.text)
    if cfg.ocr.save_tsv:
        atomic_write_text(paths.page_tsv(n), res.tsv)
    out = res.summary() | {"seconds": round(time.monotonic() - t0, 3), "text_file": f"ocr/page_{n:04d}.txt"}
    if pdf is not None and width_pt:
        data = scale_single_page(pdf, width_pt, width_pt * img.height / img.width)
        dst = paths.page_pdf(n)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dst)
        out["page_pdf"] = f"ocr/pdf/page_{n:04d}.pdf"
        out["page_pdf_width_pt"] = round(width_pt, 2)
    return out


class OcrPipeline:
    def __init__(self, cfg: HarnessConfig, paths: RunPaths, manifest: Manifest,
                 width_fn: Optional[WidthFn] = None):
        self.cfg = cfg
        self.paths = paths
        self.manifest = manifest
        self.width_fn = width_fn
        self.workers = cfg.ocr.workers or default_workers()
        if self.workers > 1:
            os.environ.setdefault("OMP_THREAD_LIMIT", "1")
        self.pool = ThreadPoolExecutor(self.workers, thread_name_prefix="ocr")
        self.pending: dict[int, Future] = {}
        self.log = get_logger()
        self.done = 0
        self.failed: list[int] = []

    def submit(self, n: int) -> None:
        if n in self.pending:
            return
        rec = self.manifest.page(n)
        if not rec or rec.get("status") != "captured":
            return
        w, h = rec.get("size") or (0, 0)
        width = self.width_fn(n, w, h) if (self.width_fn and w) else None
        self.pending[n] = self.pool.submit(_job, n, str(self.paths.root / rec["image"]), self.cfg,
                                           self.paths, width)

    def poll(self) -> int:
        """Record finished jobs (main thread only). Returns how many were recorded."""
        recorded = 0
        for n, fut in list(self.pending.items()):
            if not fut.done():
                continue
            del self.pending[n]
            rec = self.manifest.page(n)
            try:
                rec["ocr"] = fut.result()
                rec.pop("ocr_error", None)
                self.done += 1
                event(self.log, "page_ocr", f"page {n}: {rec['ocr']['words']} words, "
                      f"mean conf {rec['ocr']['mean_conf']}", level=10, page=n, flags=rec["ocr"]["flags"])
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the run
                rec["ocr_error"] = f"{type(exc).__name__}: {exc}"
                self.failed.append(n)
                event(self.log, "page_ocr_failed", rec["ocr_error"], level=30, page=n)
            self.manifest.set_page(n, rec)
            recorded += 1
        return recorded

    def drain(self, check: Optional[Callable[[], None]] = None, progress: Optional[Callable[[int, int], None]] = None
              ) -> None:
        total = len(self.pending)
        while self.pending:
            if check:
                check()
            wait(list(self.pending.values()), timeout=0.5, return_when=FIRST_COMPLETED)
            self.poll()
            if progress:
                progress(total - len(self.pending), total)

    def close(self, cancel: bool = False) -> None:
        self.pool.shutdown(wait=not cancel, cancel_futures=cancel)
