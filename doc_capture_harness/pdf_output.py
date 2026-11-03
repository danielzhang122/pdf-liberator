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


def _page_pdf_bytes(img: Image.Image, cfg: HarnessConfig, text_layer: bool, dpi: int) -> bytes:
    """One-page PDF for ``img``: with a Tesseract text layer, or image-only."""
    fmt = cfg.pdf.image_format
    if not text_layer:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PDF", resolution=float(dpi),
                                **({"quality": cfg.pdf.jpeg_quality} if fmt == "jpeg" else {}))
        return buf.getvalue()
    import pytesseract

    with tempfile.TemporaryDirectory(prefix="dch_pdf_") as td:
        if fmt == "jpeg":
            src = Path(td) / "page.jpg"
            img.convert("RGB").save(src, "JPEG", quality=cfg.pdf.jpeg_quality, dpi=(dpi, dpi))
        else:
            src = Path(td) / "page.png"
            img.save(src, "PNG", dpi=(dpi, dpi))
        o = cfg.ocr
        tess_cfg = f"--oem {o.oem} --psm {o.psm} {o.extra_config}".strip()
        return pytesseract.image_to_pdf_or_hocr(str(src), extension="pdf", lang=o.lang, config=tess_cfg)


def _target_size(page_no: int, img: Image.Image, cfg: HarnessConfig,
                 orig: Optional[OriginalInfo]) -> tuple[float, float, str]:
    """Width/height in points for this page, keeping the capture's aspect ratio."""
    mode = cfg.pdf.page_size
    base, src = None, mode
    if mode == "original" and orig is not None:
        base = orig.size_for(page_no)
        src = "original" if base else "fallback"
    if base is None and mode in ("original", "letter", "a4"):
        base = PAGE_SIZES_PT[cfg.pdf.fallback_page_size if mode == "original" else mode]
        if mode == "original":
            src = f"fallback:{cfg.pdf.fallback_page_size}"
    if base is None:   # "pixels": capture resolution at fallback_dpi
        return img.width * 72.0 / cfg.pdf.pixels_dpi, img.height * 72.0 / cfg.pdf.pixels_dpi, "pixels"
    # Match the target width; height follows the capture's aspect ratio (the
    # crop includes a few pixels of padding, so it is close to the page's own).
    w = base[0]
    return w, w * img.height / img.width, src


def output_paths(cfg: HarnessConfig, paths: RunPaths, document: dict,
                 explicit: Optional[Path] = None, next_to_original: Optional[bool] = None) -> list[Path]:
    original = Path(document["path"]).resolve()
    name = cfg.pdf.output_name or f"{original.stem}.ocr.pdf"
    outs = [Path(explicit)] if explicit else [paths.root / "output" / name]
    if (cfg.pdf.copy_next_to_original if next_to_original is None else next_to_original):
        outs.append(original.parent / name)
    protected = {original, (paths.work / original.name).resolve()}
    for p in outs:
        if p.resolve() in protected:
            raise HarnessError(f"refusing to write the PDF over the original document: {p}")
    return outs


def build_pdf(cfg: HarnessConfig, manifest: Manifest, paths: RunPaths, *,
              explicit_output: Optional[Path] = None, next_to_original: Optional[bool] = None,
              overwrite: Optional[bool] = None, estop=None) -> dict:
    from pypdf import PdfReader, PdfWriter

    log = get_logger()
    t0 = time.monotonic()
    doc = manifest.data["document"]
    outs = output_paths(cfg, paths, doc, explicit_output, next_to_original)
    overwrite = cfg.pdf.overwrite if overwrite is None else overwrite
    run_out = paths.root / "output"
    for p in outs:
        # Our own run directory output is always replaceable; anything else needs --overwrite.
        if p.exists() and not overwrite and run_out.resolve() not in p.resolve().parents:
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

    orig = OriginalInfo(Path(doc["path"])) if (cfg.pdf.page_size == "original" or cfg.pdf.copy_metadata) else None
    if orig is not None and orig.encrypted:
        event(log, "pdf_original_encrypted", "original PDF is encrypted; not decrypting it, so its page "
              "sizes and metadata are not used", level=30)

    nums = manifest.page_numbers()
    last = max(nums, default=0)
    expected = (manifest.data.get("expected_pages") or {}).get("count")
    writer = PdfWriter()
    pages_info = []
    ref_img = None
    for n in range(1, last + 1):
        if estop is not None:
            estop.check()
        rec = manifest.page(n)
        captured = rec is not None and rec.get("status") == "captured"
        if captured:
            img = Image.open(paths.root / rec["image"]).convert("RGB")
            ref_img = img
            up = float((rec.get("ocr") or {}).get("upscale") or 1.0)
            if text_layer and up > 1.0:   # same resolution the per-page OCR used
                img = img.resize((int(img.width * up), int(img.height * up)), Image.LANCZOS)
        elif cfg.pdf.missing_pages == "skip":
            pages_info.append({"page": n, "pdf_page": None, "status": "skipped"})
            continue
        else:
            img = _placeholder(n, "missing" if rec is None else rec.get("status", "failed"), ref_img)
        w_pt, h_pt, size_src = _target_size(n, img, cfg, orig)
        dpi = max(72, min(1200, round(img.width * 72.0 / w_pt)))
        data = _page_pdf_bytes(img, cfg, text_layer, dpi)
        added = writer.add_page(PdfReader(io.BytesIO(data)).pages[0])
        added.scale_to(w_pt, h_pt)        # scale after attaching to the writer (pypdf >= 5)
        pages_info.append({"page": n, "pdf_page": len(writer.pages), "status": "captured" if captured else "placeholder",
                           "size_pt": [round(w_pt, 1), round(h_pt, 1)], "size_source": size_src})

    if not writer.pages:
        raise HarnessError("no pages to write")

    meta = {}
    if cfg.pdf.copy_metadata and orig is not None and orig.readable:
        meta.update({k: v for k, v in orig.metadata.items() if k.startswith("/")})
    meta.setdefault("/Title", Path(doc["path"]).stem)
    meta.update({
        "/Producer": f"doc_capture_harness {__version__} (pypdf, Tesseract)",
        "/Creator": "doc_capture_harness",
        "/ModDate": time.strftime("D:%Y%m%d%H%M%S"),
        "/CaptureSourceSHA256": doc["sha256"],
        "/CaptureRunId": manifest.data["run_id"],
        "/CaptureNote": ("Pages are screen captures of the rendered document; the text layer is OCR "
                         "and may contain errors." if text_layer else "Pages are screen captures; no text layer."),
    })
    writer.add_metadata(meta)
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()

    written = []
    for p in outs:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        written.append(str(p))

    verification = verify_pdf(Path(written[0]), pages_info, manifest, text_layer)
    result = {
        "created": now_iso(),
        "files": written,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "pages": len(writer.pages),
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
    if orig is not None and orig.readable and orig.page_count is not None and orig.page_count != len(writer.pages):
        result["page_count_note"] = (f"original file has {orig.page_count} page(s), the searchable PDF has "
                                     f"{len(writer.pages)} (expected for wrapper documents whose file holds a "
                                     "placeholder page)")
    manifest.data["pdf"] = result
    manifest.save()
    event(log, "pdf_written", f"searchable PDF: {written[0]}", pages=result["pages"], text_layer=text_layer,
          bytes=len(data), verified=verification["ok"])
    return result


def verify_pdf(path: Path, pages_info: list[dict], manifest: Manifest, text_layer: bool) -> dict:
    """Re-open the written PDF: page count, and that OCR'd pages have extractable text."""
    from pypdf import PdfReader

    r = PdfReader(str(path))
    expected_pages = sum(1 for i in pages_info if i["pdf_page"])
    problems = []
    if len(r.pages) != expected_pages:
        problems.append(f"page count {len(r.pages)} != {expected_pages}")
    with_text = 0
    for info in pages_info:
        if not info["pdf_page"] or info["status"] != "captured":
            continue
        words = ((manifest.page(info["page"]) or {}).get("ocr") or {}).get("words") or 0
        txt = r.pages[info["pdf_page"] - 1].extract_text() or ""
        if txt.strip():
            with_text += 1
        elif text_layer and words > 0:
            problems.append(f"page {info['page']}: OCR found {words} words but the PDF page has no text layer")
    return {"ok": not problems, "pages_with_text": with_text, "problems": problems}
