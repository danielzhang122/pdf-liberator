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


def _ocr_once(img: Image.Image, cfg: OcrConfig, upscale: float):
    import pytesseract

    work = img.convert("RGB")
    if upscale and upscale != 1.0:
        work = work.resize((int(work.width * upscale), int(work.height * upscale)), Image.LANCZOS)
    tess_cfg = f"--oem {cfg.oem} --psm {cfg.psm} {cfg.extra_config}".strip()
    data = pytesseract.image_to_data(work, lang=cfg.lang, config=tess_cfg,
                                     output_type=pytesseract.Output.DICT)
    return data


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


def ocr_image(img: Image.Image, cfg: OcrConfig) -> OcrResult:
    upscale = cfg.upscale or 1.0
    data = _ocr_once(img, cfg, upscale)
    text, confs, heights, tsv = _assemble(data)
    med_h = statistics.median(heights) / upscale if heights else None
    # Small text OCRs badly; retry once at 2x when it helps.
    if med_h is not None and med_h < cfg.auto_upscale_min_word_height and upscale < 2.0:
        data2 = _ocr_once(img, cfg, 2.0)
        text2, confs2, heights2, tsv2 = _assemble(data2)
        if confs2 and (not confs or statistics.mean(confs2) > statistics.mean(confs)):
            text, confs, heights, tsv, upscale = text2, confs2, heights2, tsv2, 2.0

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


def quick_text(img: Image.Image, cfg: OcrConfig) -> str:
    """Fast, low-fidelity OCR used only for placeholder / indicator checks."""
    import pytesseract

    return pytesseract.image_to_string(img.convert("L"), lang=cfg.lang, config="--psm 6")
