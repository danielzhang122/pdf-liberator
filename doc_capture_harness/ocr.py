"""Tesseract OCR via pytesseract, with per-page confidence statistics.

Everything runs locally: pytesseract shells out to the local tesseract binary.
"""
from __future__ import annotations

import os
import shutil
import statistics
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from PIL import Image

from .config import OcrConfig
from .models import Flag

WINDOWS_TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]


def resolve_tesseract(cfg: OcrConfig) -> str:
    import pytesseract

    candidates = [cfg.tesseract_cmd] if cfg.tesseract_cmd else []
    candidates.append(shutil.which("tesseract"))
    if sys.platform == "win32":
        candidates += WINDOWS_TESSERACT_CANDIDATES
    for c in candidates:
        if c and Path(c).exists():
            pytesseract.pytesseract.tesseract_cmd = c
            return c
    raise FileNotFoundError(
        "tesseract executable not found. Install Tesseract OCR and/or set ocr.tesseract_cmd "
        "(see README: 'Tesseract setup')."
    )


@dataclass
class OcrResult:
    text: str
    words: int
    mean_conf: Optional[float]
    median_conf: Optional[float]
    min_conf: Optional[float]
    p10_conf: Optional[float]
    low_conf_words: int
    low_conf_ratio: float
    median_word_height_px: Optional[float]
    upscale: float
    flags: list[str] = field(default_factory=list)
    tsv: str = ""

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("text")
        d.pop("tsv")
        return d


def _assemble(data: dict) -> tuple[str, list[float], list[float], str]:
    lines: list[str] = []
    current_key, current_words = None, []
    last_block = None
    confs, heights = [], []
    tsv_rows = ["block\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext"]
    n = len(data["text"])
    for i in range(n):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key != current_key:
            if current_words:
                lines.append(" ".join(current_words))
            if last_block is not None and data["block_num"][i] != last_block:
                lines.append("")
            current_key, current_words = key, []
            last_block = data["block_num"][i]
        current_words.append(word)
        conf = float(data["conf"][i])
        if conf >= 0:
            confs.append(conf)
            heights.append(float(data["height"][i]))
        tsv_rows.append("\t".join(str(data[k][i]) for k in
                                  ("block_num", "par_num", "line_num", "word_num", "left", "top",
                                   "width", "height", "conf")) + "\t" + word)
    if current_words:
        lines.append(" ".join(current_words))
    return "\n".join(lines).strip() + "\n", confs, heights, "\n".join(tsv_rows) + "\n"


def _parse_tsv(tsv: str) -> dict:
    """Tesseract TSV -> the dict layout of ``pytesseract.image_to_data(..., DICT)``."""
    keys = ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
            "left", "top", "width", "height", "conf", "text"]
    out: dict = {k: [] for k in keys}
    lines = tsv.splitlines()
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 11:
            continue
        parts += [""] * (12 - len(parts))
        for k, v in zip(keys, parts[:12]):
            if k == "text":
                out[k].append(v)
            elif k == "conf":
                out[k].append(float(v) if v not in ("", "-1") else -1.0)
            else:
                out[k].append(int(v) if v.lstrip("-").isdigit() else 0)
    return out


def run_tesseract(img: Image.Image, cfg: OcrConfig, upscale: float, *, want_pdf: bool = False,
                  dpi: int = 300, image_format: str = "png", jpeg_quality: int = 85,
                  timeout_s: float = 600.0) -> tuple[dict, Optional[bytes]]:
    """One Tesseract invocation producing word data (TSV) and, optionally, a
    one-page searchable PDF (page image + invisible text layer)."""
    import subprocess
    import tempfile

    import pytesseract

    cmd = pytesseract.pytesseract.tesseract_cmd
    work = img.convert("RGB")
    if upscale and upscale != 1.0:
        work = work.resize((int(work.width * upscale), int(work.height * upscale)), Image.LANCZOS)
        dpi = int(round(dpi * upscale))
    dpi = max(72, min(2400, int(dpi)))
    with tempfile.TemporaryDirectory(prefix="dch_ocr_") as td:
        if image_format == "jpeg":
            src = os.path.join(td, "in.jpg")
            work.save(src, "JPEG", quality=jpeg_quality, dpi=(dpi, dpi))
        else:
            src = os.path.join(td, "in.png")
            work.save(src, "PNG", dpi=(dpi, dpi))
        base = os.path.join(td, "out")
        args = [cmd, src, base, "-l", cfg.lang, "--psm", str(cfg.psm), "--oem", str(cfg.oem),
                "--dpi", str(dpi), *cfg.extra_config.split(), "tsv"]
        if want_pdf:
            args.append("pdf")
        # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS: OCR never starves the reader's rendering.
        flags = (0x08000000 | 0x00004000) if sys.platform == "win32" else 0
        proc = subprocess.run(args, capture_output=True, timeout=timeout_s, creationflags=flags)
        if proc.returncode != 0:
            raise RuntimeError(f"tesseract failed ({proc.returncode}): "
                               f"{proc.stderr.decode(errors='replace')[-500:]}")
        with open(base + ".tsv", encoding="utf-8") as f:
            data = _parse_tsv(f.read())
        pdf = None
        if want_pdf:
            with open(base + ".pdf", "rb") as f:
                pdf = f.read()
    return data, pdf


def _summarise(data: dict, cfg: OcrConfig, upscale: float):
    text, confs, heights, tsv = _assemble(data)
    med_h = statistics.median(heights) / upscale if heights else None
    return text, confs, heights, tsv, med_h


def _result(text, confs, heights, tsv, upscale, cfg: OcrConfig) -> OcrResult:
    flags: list[str] = []
    if confs:
        mean_c = statistics.mean(confs)
        sorted_c = sorted(confs)
        p10 = sorted_c[max(0, int(len(sorted_c) * 0.1) - 1)] if len(sorted_c) >= 10 else sorted_c[0]
        low = sum(1 for c in confs if c < cfg.low_word_conf)
        ratio = low / len(confs)
        if mean_c < cfg.page_min_mean_conf or ratio > cfg.page_max_low_conf_ratio:
            flags.append(Flag.LOW_CONFIDENCE)
        med_h_orig = statistics.median(heights) / upscale
        if med_h_orig < cfg.min_word_height_px:
            flags.append(Flag.LOW_RESOLUTION)
        return OcrResult(text, len(confs), round(mean_c, 2), round(statistics.median(confs), 2),
                         round(sorted_c[0], 2), round(p10, 2), low, round(ratio, 4),
                         round(med_h_orig, 1), upscale, flags, tsv)
    flags.append(Flag.NO_TEXT)
    return OcrResult(text if text.strip() else "", 0, None, None, None, None, 0, 0.0, None, upscale, flags, tsv)


def ocr_image(img: Image.Image, cfg: OcrConfig, *, pdf_opts: Optional[dict] = None
              ) -> tuple[OcrResult, Optional[bytes]]:
    """OCR one page. With ``pdf_opts`` the same Tesseract pass also renders the
    page's searchable PDF. Small text is retried once at 2x and the better
    result (by mean confidence) is kept, together with its PDF."""
    want_pdf = pdf_opts is not None
    opts = pdf_opts or {}
    dpi = opts.get("dpi", 300)
    fmt = opts.get("image_format", "png")
    q = opts.get("jpeg_quality", 85)
    upscale = cfg.upscale or 1.0
    data, pdf = run_tesseract(img, cfg, upscale, want_pdf=want_pdf, dpi=dpi, image_format=fmt, jpeg_quality=q)
    text, confs, heights, tsv, med_h = _summarise(data, cfg, upscale)
    if med_h is not None and med_h < cfg.auto_upscale_min_word_height and upscale < 2.0:
        data2, pdf2 = run_tesseract(img, cfg, 2.0, want_pdf=want_pdf, dpi=dpi, image_format=fmt, jpeg_quality=q)
        t2, c2, h2, tsv2, _ = _summarise(data2, cfg, 2.0)
        if c2 and (not confs or statistics.mean(c2) > statistics.mean(confs)):
            text, confs, heights, tsv, upscale, pdf = t2, c2, h2, tsv2, 2.0, pdf2
    return _result(text, confs, heights, tsv, upscale, cfg), pdf


def quick_text(img: Image.Image, cfg: OcrConfig) -> str:
    """Fast, low-fidelity OCR used only for placeholder / indicator checks."""
    import pytesseract

    return pytesseract.image_to_string(img.convert("L"), lang=cfg.lang, config="--psm 6")
