"""`doctor`: check that this machine is ready, with a fix for every problem."""
from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


def _check_imports(results):
    pkgs = [("PIL", "Pillow"), ("numpy", "numpy"), ("pytesseract", "pytesseract"), ("yaml", "PyYAML"),
            ("pypdf", "pypdf"), ("pikepdf", "pikepdf"), ("mss", "mss")]
    if sys.platform == "win32":
        pkgs += [("pywinauto", "pywinauto"), ("win32api", "pywin32"), ("pyautogui", "pyautogui")]
    missing = []
    for mod, pip_name in pkgs:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(pip_name)
    if missing:
        results.append((FAIL, "Python packages", "missing: " + ", ".join(missing),
                        "run install.ps1, or: python -m pip install -r requirements.txt"))
    else:
        results.append((OK, "Python packages", "all installed", ""))


def _check_python(results):
    v = sys.version_info
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    if v >= (3, 14):
        results.append((FAIL, "Python", f"{v.major}.{v.minor} is too new for some packages",
                        "use Python 3.12 (install.ps1 sets this up automatically)"))
    elif v < (3, 10):
        results.append((FAIL, "Python", f"{v.major}.{v.minor} ({bits}-bit)", "install Python 3.10 or newer (64-bit)"))
    elif bits != 64:
        results.append((WARN, "Python", f"{v.major}.{v.minor} 32-bit", "64-bit Python is recommended for large documents"))
    else:
        results.append((OK, "Python", f"{v.major}.{v.minor}.{v.micro} 64-bit", ""))


def _check_tesseract(results, cfg):
    try:
        from .ocr import resolve_tesseract
        cmd = resolve_tesseract(cfg.ocr)
    except Exception:
        results.append((FAIL, "Tesseract OCR", "not found",
                        "winget install --id UB-Mannheim.TesseractOCR   (or set ocr.tesseract_cmd)"))
        return
    import subprocess
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=30)
        version = (out.stdout or out.stderr).splitlines()[0]
        langs = subprocess.run([cmd, "--list-langs"], capture_output=True, text=True, timeout=30)
        have = {line.strip() for line in (langs.stdout or "").splitlines()[1:]}
    except Exception as exc:
        results.append((FAIL, "Tesseract OCR", f"{cmd} does not run: {exc}", "reinstall Tesseract"))
        return
    results.append((OK, "Tesseract OCR", f"{version} at {cmd}", ""))
    want = [x for x in cfg.ocr.lang.split("+") if x]
    missing = [x for x in want if x not in have]
    if missing:
        results.append((FAIL, "OCR languages", f"missing {', '.join(missing)} (installed: {', '.join(sorted(have))})",
                        "copy <lang>.traineddata into Tesseract's tessdata folder, or rerun its installer"))
    else:
        results.append((OK, "OCR languages", ", ".join(want), ""))
    configs = Path(cmd).parent / "tessdata" / "configs"
    if sys.platform == "win32" and configs.is_dir() and not (configs / "pdf").exists():
        results.append((FAIL, "Tesseract PDF output", "tessdata/configs/pdf missing", "reinstall Tesseract"))


def _check_reader(results, raw):
    from .viewers.profiles import PROFILES, detect_installed_profile
    import os

    profile = (raw.get("viewer") or {}).get("profile", "auto")
    exe = (raw.get("viewer") or {}).get("executable")
    if exe:
        ok = os.path.exists(exe)
        results.append((OK if ok else FAIL, "Reader", exe, "" if ok else "fix viewer.executable"))
        return
    if profile in ("auto", None):
        found = detect_installed_profile()
        if found:
            results.append((OK, "Reader", f"{found} (auto-detected)", ""))
        else:
            results.append((FAIL, "Reader", "Adobe Acrobat/Reader or PDF-XChange not found",
                            "install a supported reader, or set viewer.profile: generic + viewer.executable"))
        return
    cands = [c for c in PROFILES.get(profile, {}).get("executable_candidates", []) if os.path.exists(c)]
    results.append((OK if cands else FAIL, "Reader", f"{profile}: {cands[0] if cands else 'not found'}",
                    "" if cands else "install it or set viewer.executable"))


def _check_display(results):
    if sys.platform != "win32":
        results.append((WARN, "Desktop automation", "not Windows: only `ocr`, `pdf`, `report` and "
                        "`batch --dry-run` work here", "run captures on Windows"))
        return
    from . import platform_win as pw
    mode = pw.current_dpi_awareness()
    results.append((OK if mode.startswith("per_monitor") else WARN, "DPI awareness", mode,
                    "" if mode.startswith("per_monitor") else "captures may be misaligned on scaled displays"))
    mons = pw.monitors()
    scales = sorted({m.scale for m in mons if m.scale})
    desc = "; ".join(f"{m.device} {m.rect.width}x{m.rect.height} @{int((m.scale or 1) * 100)}%"
                     + (" (primary)" if m.primary else "") for m in mons)
    if len(scales) > 1:
        results.append((WARN, "Monitors", desc, "keep the reader on one monitor; mixed scaling across monitors "
                        "is supported but the window must not span them"))
    else:
        results.append((OK, "Monitors", desc, ""))
    small = [m for m in mons if m.primary and m.rect.height < 1000]
    if small:
        results.append((WARN, "Screen size", f"primary height {small[0].rect.height}px",
                        "a taller screen (or portrait monitor) gives larger pages and better OCR"))
    if not pw.desktop_is_interactive():
        results.append((FAIL, "Interactive desktop", "desktop is locked or not interactive",
                        "run in an unlocked, visible desktop session; disable screen lock and sleep during batches"))
    else:
        results.append((OK, "Interactive desktop", "available", ""))
    results.append((WARN, "Power / lock settings", "cannot be checked automatically",
                    "for long batches: disable sleep, screensaver and automatic screen lock; keep RDP sessions open "
                    "and not minimised"))


def _check_disk(results, out_dir: Path):
    try:
        p = out_dir if out_dir.exists() else out_dir.parent
        free = shutil.disk_usage(p).free / 1e9
    except OSError:
        return
    # ~1-2 MB per page for PNG + PDF; 10 GB covers several thousand pages.
    level = OK if free >= 20 else WARN if free >= 5 else FAIL
    results.append((level, "Free disk space", f"{free:.0f} GB at {p}",
                    "" if level == OK else "large documents need about 2 MB per page"))


def run_doctor(raw: dict, out_dir: Path, echo=print) -> int:
    from .config import build_config, deep_merge

    cfg_raw = deep_merge(raw, {"viewer": {"profile": "generic"}}) if (raw.get("viewer") or {}).get(
        "profile", "auto") == "auto" else raw
    cfg = build_config(cfg_raw)
    results: list[tuple[str, str, str, str]] = []
    if sys.platform == "win32":
        # Must happen before pywinauto is imported (it sets system-level awareness).
        from . import platform_win
        platform_win.enable_dpi_awareness()
    _check_python(results)
    _check_imports(results)
    _check_tesseract(results, cfg)
    _check_reader(results, raw)
    _check_display(results)
    _check_disk(results, out_dir)
    width = max(len(r[1]) for r in results)
    for status, label, detail, fix in results:
        echo(f"[{status}] {label:<{width}}  {detail}")
        if fix and status != OK:
            echo(f"       {'':<{width}}  -> {fix}")
    fails = sum(1 for r in results if r[0] == FAIL)
    echo("\nReady." if not fails else f"\n{fails} problem(s) to fix before capturing.")
    return 1 if fails else 0
