"""Assemble the captured pages into a searchable PDF.

Each captured PNG becomes one PDF page: the page image, plus an invisible text
layer from Tesseract's PDF renderer, positioned over the words so the result
can be searched, selected and copied. Pages are merged in order with pypdf and
scaled to the original document's page size where it can be read.

The original document is never modified. Output goes to
``<run>/output/<stem>.ocr.pdf`` and, if requested, to a *new* file next to the
original. Writing to the original's own path is refused.

If the original PDF is encrypted, it is not decrypted. Its page sizes and
metadata are then not used: pages fall back to ``pdf.fallback_page_size``.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

from . import __version__
from .config import HarnessConfig
from .errors import HarnessError
from .logging_utils import event, get_logger
from .state import Manifest, RunPaths, now_iso

PAGE_SIZES_PT = {"letter": (612.0, 792.0), "a4": (595.276, 841.89)}


class OriginalInfo:
    """Page sizes + metadata of the original PDF, if readable without decryption."""

    def __init__(self, path: Path):
        self.path = path
        self.readable = False
        self.encrypted = False
        self.page_count: Optional[int] = None
        self.sizes: list[tuple[float, float]] = []
        self.metadata: dict = {}
        self.error: Optional[str] = None
        import logging
        pypdf_log = logging.getLogger("pypdf")
        old_level = pypdf_log.level
        pypdf_log.setLevel(logging.ERROR)     # wrapper files are often not clean PDFs
        try:
            from pypdf import PdfReader
            r = PdfReader(str(path))
            if r.is_encrypted:
                self.encrypted = True
                return
            self.page_count = len(r.pages)
            for p in r.pages:
                box = p.mediabox
                w, h = float(box.width), float(box.height)
                if int(p.get("/Rotate", 0) or 0) % 180:
                    w, h = h, w
                self.sizes.append((w, h))
            if r.metadata:
                self.metadata = {k: str(v) for k, v in r.metadata.items() if v is not None}
            self.readable = True
        except Exception as exc:   # not a PDF, damaged, unsupported wrapper format...
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            pypdf_log.setLevel(old_level)

    def page_labels(self) -> Optional[list[str]]:
        """Page labels (e.g. i, ii, 1, 2); None if unreadable or encrypted."""
        if not self.readable:
            return None
        import logging
        pypdf_log = logging.getLogger("pypdf")
        old = pypdf_log.level
        pypdf_log.setLevel(logging.ERROR)
        try:
            from pypdf import PdfReader
            return [str(x) for x in PdfReader(str(self.path)).page_labels]
        except Exception:
            return None
        finally:
            pypdf_log.setLevel(old)

    def size_for(self, page_no: int) -> Optional[tuple[float, float]]:
        if not self.sizes:
            return None
        return self.sizes[min(page_no, len(self.sizes)) - 1]


def _placeholder(page_no: int, status: str, ref: Optional[Image.Image]) -> Image.Image:
    size = ref.size if ref is not None else (1275, 1650)
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    try:
        from PIL import ImageFont
        font = ImageFont.load_default(size=max(18, size[0] // 30))
    except Exception:
        font = None
    d.text((size[0] // 12, size[1] // 3), f"Page {page_no} was not captured ({status}).",
           fill="black", font=font)
    d.rectangle([4, 4, size[0] - 5, size[1] - 5], outline=(200, 0, 0), width=4)
    return img


def scale_single_page(pdf_bytes: bytes, w_pt: float, h_pt: float) -> bytes:
    """Scale a one-page PDF (image + text layer together) to exactly w x h points."""
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter()
    page = w.add_page(PdfReader(io.BytesIO(pdf_bytes)).pages[0])
    page.scale_to(w_pt, h_pt)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def target_width(page_no: int, img_w: int, cfg: HarnessConfig, orig: Optional[OriginalInfo]) -> tuple[float, str]:
    """PDF page width in points for a capture; the height follows its aspect ratio
    (the crop includes a few pixels of padding, so it is close to the page's own)."""
    mode = cfg.pdf.page_size
    if mode == "pixels":
        return img_w * 72.0 / cfg.pdf.pixels_dpi, "pixels"
    if mode == "original":
        size = orig.size_for(page_no) if orig is not None else None
        if size:
            return size[0], "original"
        return PAGE_SIZES_PT[cfg.pdf.fallback_page_size][0], f"fallback:{cfg.pdf.fallback_page_size}"
    return PAGE_SIZES_PT[mode][0], mode


def make_width_fn(cfg: HarnessConfig, orig: Optional[OriginalInfo]):
    """For the OCR pipeline: which width to render each page's PDF at (None = no PDF)."""
    if not (cfg.pdf.enabled and cfg.pdf.text_layer):
        return None
    return lambda n, w, h: target_width(n, w, cfg, orig)[0]


def _make_page_pdf(img: Image.Image, cfg: HarnessConfig, text_layer: bool, w_pt: float) -> bytes:
    h_pt = w_pt * img.height / img.width
    dpi = max(72, min(2400, round(img.width * 72.0 / w_pt)))
    if text_layer:
        from .ocr import run_tesseract
        _, data = run_tesseract(img, cfg.ocr, 1.0, want_pdf=True, dpi=dpi, image_format=cfg.pdf.image_format,
                                jpeg_quality=cfg.pdf.jpeg_quality)
    else:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PDF", resolution=float(dpi),
                                **({"quality": cfg.pdf.jpeg_quality} if cfg.pdf.image_format == "jpeg" else {}))
        data = buf.getvalue()
    return scale_single_page(data, w_pt, h_pt)


def output_paths(cfg: HarnessConfig, paths: RunPaths, document: dict,
                 explicit: Optional[Path] = None, next_to_original: Optional[bool] = None) -> list[Path]:
    original = Path(document["path"]).resolve()
    name = cfg.pdf.output_name or f"{original.stem}.ocr.pdf"
    if explicit:
        outs = [Path(explicit)]
    elif cfg.pdf.output_path:
        outs = [Path(cfg.pdf.output_path)]
    else:
        outs = [paths.root / "output" / name]
    if (cfg.pdf.copy_next_to_original if next_to_original is None else next_to_original):
        outs.append(original.parent / name)
    protected = {original, (paths.work / original.name).resolve()}
    for p in outs:
        if p.resolve() in protected:
            raise HarnessError(f"refusing to write the PDF over the original document: {p}")
    return outs


def merge_pdfs(files: list[Path], out: Path, metadata: dict, chunk: int = 200) -> None:
    """Merge one-page PDFs with qpdf (pikepdf), in chunks so that only ``chunk``
    source files are open at a time and memory stays flat for long documents."""
    import pikepdf

    out.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="dch_merge_", dir=str(out.parent)))
    try:
        level, items = 0, list(files)
        while True:
            groups = [items[i:i + chunk] for i in range(0, len(items), chunk)]
            final = len(groups) == 1
            next_items = []
            for gi, group in enumerate(groups):
                dst = pikepdf.new()
                srcs = [pikepdf.open(str(f)) for f in group]
                try:
                    for src in srcs:
                        dst.pages.extend(src.pages)
                    target = out.with_name(out.name + ".tmp") if final else tmpdir / f"l{level}_{gi:05d}.pdf"
                    if final:
                        for k, v in metadata.items():
                            dst.docinfo[k] = pikepdf.String(str(v))
                    dst.save(str(target), compress_streams=True)
                finally:
                    for src in srcs:
                        src.close()
                    dst.close()
                next_items.append(target)
            if final:
                os.replace(next_items[0], out)
                return
            items, level = next_items, level + 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def build_pdf(cfg: HarnessConfig, manifest: Manifest, paths: RunPaths, *,
              explicit_output: Optional[Path] = None, next_to_original: Optional[bool] = None,
              overwrite: Optional[bool] = None, estop=None, orig: Optional[OriginalInfo] = None) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    log = get_logger()
    t0 = time.monotonic()
    doc = manifest.data["document"]
    outs = output_paths(cfg, paths, doc, explicit_output, next_to_original)
    overwrite = cfg.pdf.overwrite if overwrite is None else overwrite
    run_out = (paths.root / "output").resolve()
    for p in outs:
        # Our own run-directory output is always replaceable; anything else needs --overwrite.
        if p.exists() and not overwrite and run_out not in p.resolve().parents:
            raise HarnessError(f"{p} already exists; pass --overwrite (or set pdf.overwrite) to replace it")

    text_layer = cfg.pdf.text_layer
    note = None
    if text_layer:
        try:
            from .ocr import resolve_tesseract
            resolve_tesseract(cfg.ocr)
        except Exception as exc:
            text_layer = False
            note = f"tesseract unavailable, image-only PDF: {exc}"
            event(log, "pdf_text_layer_disabled", note, level=30)

    if orig is None and (cfg.pdf.page_size == "original" or cfg.pdf.copy_metadata):
        orig = OriginalInfo(Path(doc["path"]))
    if orig is not None and orig.encrypted:
        event(log, "pdf_original_encrypted", "original PDF is encrypted; not decrypting it, so its page "
              "sizes and metadata are not used", level=30)

    nums = manifest.page_numbers()
    last = max(nums, default=0)
    expected = (manifest.data.get("expected_pages") or {}).get("count")
    paths.page_pdfs.mkdir(parents=True, exist_ok=True)
    pages_info: list[dict] = []
    files: list[Path] = []
    to_make: list[tuple[int, Path, Optional[Path], str, Optional[tuple[int, int]]]] = []
    ref_size: Optional[tuple[int, int]] = None
    reused = 0
    for n in range(1, last + 1):
        rec = manifest.page(n)
        captured = rec is not None and rec.get("status") == "captured"
        if not captured and cfg.pdf.missing_pages == "skip":
            pages_info.append({"page": n, "pdf_page": None, "status": "skipped"})
            continue
        if captured:
            size = tuple(rec.get("size") or (0, 0))
            ref_size = size or ref_size
            w_pt, src = target_width(n, size[0] or 1, cfg, orig)
            o = rec.get("ocr") or {}
            cached = paths.root / o["page_pdf"] if o.get("page_pdf") else None
            if (text_layer and cached is not None and cached.exists()
                    and abs(float(o.get("page_pdf_width_pt", 0)) - w_pt) < 0.01):
                path = cached
                reused += 1
            else:
                path = paths.page_pdfs / f"build_{n:04d}.pdf"
                to_make.append((n, path, paths.root / rec["image"], "captured", None))
            status = "captured"
        else:
            w_pt, src = target_width(n, (ref_size or (1275, 1650))[0], cfg, orig)
            path = paths.page_pdfs / f"placeholder_{n:04d}.pdf"
            to_make.append((n, path, None, "missing" if rec is None else rec.get("status", "failed"), ref_size))
            status = "placeholder"
        files.append(path)
        pages_info.append({"page": n, "pdf_page": len(files), "status": status, "width_pt": round(w_pt, 1),
                           "size_source": src})

    if not files:
        raise HarnessError("no pages to write")

    def make(item):
        n, dst, png, status, ref = item
        if png is not None:
            with Image.open(png) as im:
                img = im.convert("RGB")
            tl = text_layer
        else:
            img = _placeholder(n, status, Image.new("RGB", ref) if ref else None)
            tl = False
        w_pt, _ = target_width(n, img.width, cfg, orig)
        data = _make_page_pdf(img, cfg, tl, w_pt)
        dst.write_bytes(data)

    if to_make:
        from .ocr_pipeline import default_workers
        workers = cfg.ocr.workers or default_workers()
        if workers > 1:
            os.environ.setdefault("OMP_THREAD_LIMIT", "1")
        with ThreadPoolExecutor(workers, thread_name_prefix="pdf") as pool:
            for _ in pool.map(make, to_make):
                if estop is not None:
                    estop.check()

    meta = {}
    if cfg.pdf.copy_metadata and orig is not None and orig.readable:
        meta.update({k: v for k, v in orig.metadata.items() if k.startswith("/")})
    meta.setdefault("/Title", Path(doc["path"]).stem)
    meta.update({
        "/Producer": f"doc_capture_harness {__version__} (Tesseract, qpdf)",
        "/Creator": "doc_capture_harness",
        "/ModDate": time.strftime("D:%Y%m%d%H%M%S"),
        "/CaptureSourceSHA256": doc["sha256"],
        "/CaptureRunId": manifest.data["run_id"],
        "/CaptureNote": ("Pages are screen captures of the rendered document; the text layer is OCR "
                         "and may contain errors." if text_layer else "Pages are screen captures; no text layer."),
    })
    first = outs[0]
    if estop is not None:
        estop.check()
    merge_pdfs(files, first, meta)
    for extra in outs[1:]:
        extra.parent.mkdir(parents=True, exist_ok=True)
        tmp = extra.with_name(extra.name + ".tmp")
        shutil.copyfile(first, tmp)
        os.replace(tmp, extra)
    written = [str(p) for p in outs]

    verification = verify_pdf(first, pages_info, manifest, text_layer, cfg.pdf.verify_sample_pages)
    # Temporary per-build pages are always removed; OCR-pass page PDFs only if not kept.
    for f in paths.page_pdfs.glob("build_*.pdf"):
        f.unlink(missing_ok=True)
    for f in paths.page_pdfs.glob("placeholder_*.pdf"):
        f.unlink(missing_ok=True)
    if not cfg.pdf.keep_page_pdfs:
        for f in paths.page_pdfs.glob("page_*.pdf"):
            f.unlink(missing_ok=True)
    data_len = first.stat().st_size
    result = {
        "created": now_iso(),
        "files": written,
        "bytes": data_len,
        "sha256": _sha256(first),
        "pages": len(files),
        "pages_reused_from_ocr_pass": reused,
        "placeholder_pages": [i["page"] for i in pages_info if i["status"] == "placeholder"],
        "skipped_pages": [i["page"] for i in pages_info if i["status"] == "skipped"],
        "text_layer": text_layer,
        "note": note,
        "page_size_mode": cfg.pdf.page_size,
        "original_pdf": None if orig is None else {
            "readable": orig.readable, "encrypted": orig.encrypted, "page_count": orig.page_count,
            "error": orig.error},
        "expected_pages": expected,
        "verification": verification,
        "seconds": round(time.monotonic() - t0, 3),
    }
    if orig is not None and orig.readable and orig.page_count is not None and orig.page_count != len(files):
        result["page_count_note"] = (f"original file has {orig.page_count} page(s), the searchable PDF has "
                                     f"{len(files)} (expected for wrapper documents whose file holds a "
                                     "placeholder page)")
    manifest.data["pdf"] = result
    manifest.save()
    event(log, "pdf_written", f"searchable PDF: {written[0]}", pages=result["pages"], text_layer=text_layer,
          bytes=data_len, verified=verification["ok"], reused=reused)
    return result


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_pdf(path: Path, pages_info: list[dict], manifest: Manifest, text_layer: bool,
               sample: int = 60) -> dict:
    """Re-open the written PDF: exact page count, plus extractable text on a
    sample of OCR'd pages (all of them for documents up to ``sample`` pages)."""
    from pypdf import PdfReader

    r = PdfReader(str(path))
    expected_pages = sum(1 for i in pages_info if i["pdf_page"])
    problems = []
    if len(r.pages) != expected_pages:
        problems.append(f"page count {len(r.pages)} != {expected_pages}")
    candidates = [i for i in pages_info if i["pdf_page"] and i["status"] == "captured"]
    if sample and len(candidates) > sample:
        step = len(candidates) / sample
        candidates = [candidates[int(k * step)] for k in range(sample)] + [candidates[-1]]
    with_text = 0
    for info in candidates:
        words = ((manifest.page(info["page"]) or {}).get("ocr") or {}).get("words") or 0
        if info["pdf_page"] > len(r.pages):
            continue
        txt = r.pages[info["pdf_page"] - 1].extract_text() or ""
        if txt.strip():
            with_text += 1
        elif text_layer and words > 0:
            problems.append(f"page {info['page']}: OCR found {words} words but the PDF page has no text layer")
    return {"ok": not problems, "pages_checked": len(candidates), "pages_with_text": with_text,
            "problems": problems}
