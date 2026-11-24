"""Command-line interface.

    python -m doc_capture_harness doctor
    python -m doc_capture_harness batch     C:\\Docs [--out D:\\Out] [--interactive]
    python -m doc_capture_harness run       C:\\Docs\\file.pdf   (or --config cfg.yaml)
    python -m doc_capture_harness resume    --run-dir runs/<run>
    python -m doc_capture_harness ocr       --run-dir runs/<run> [--redo]
    python -m doc_capture_harness pdf       --run-dir runs/<run> [--next-to-original]
    python -m doc_capture_harness report    --run-dir runs/<run>
    python -m doc_capture_harness calibrate --config cfg.yaml [--test-nav]
    python -m doc_capture_harness list-windows
    python -m doc_capture_harness init-config --viewer acrobat > my.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .config import HarnessConfig, _build, build_config, deep_merge, load_raw, validate
from .errors import ConfigError, HarnessError, PlatformUnsupported


# --------------------------------------------------------------------------- wiring

def _require_windows() -> None:
    if sys.platform != "win32":
        raise PlatformUnsupported("viewer automation requires Windows with an interactive desktop session; "
                                  "`ocr` and `report` work on any OS")


def _enable_dpi() -> str:
    from . import platform_win
    mode = platform_win.enable_dpi_awareness()
    os.environ["DOC_CAPTURE_DPI_MODE"] = mode
    return mode


def windows_factories(cfg: HarnessConfig):
    from .inputs import InputDriver
    from .viewers import create_adapter
    from .viewers.base import MssCaptureSource, PrintWindowCaptureSource

    def adapter_factory(checkpoint, estop):
        inputs = InputDriver(estop, cfg.safety.allow_pyautogui_fallback, cfg.safety.pyautogui_failsafe)
        return create_adapter(cfg.viewer, estop, checkpoint, inputs, cfg.prompts.ignore_title_re)

    def source_factory(adapter):
        return PrintWindowCaptureSource(adapter) if cfg.capture.method == "printwindow" else MssCaptureSource()

    return adapter_factory, source_factory


def _apply_overrides(raw: dict, args) -> dict:
    doc = raw.setdefault("document", {})
    if getattr(args, "doc", None):
        doc["path"] = args.doc
    if getattr(args, "document", None):
        doc["path"] = args.document
    if getattr(args, "time_limit", None):
        doc["time_limit_minutes"] = args.time_limit
    if getattr(args, "known_pages", None) is not None:
        doc["known_page_count"] = args.known_pages
    if getattr(args, "max_pages", None) is not None:
        doc["max_pages"] = args.max_pages
    if getattr(args, "viewer", None):
        raw.setdefault("viewer", {})["profile"] = args.viewer
    if getattr(args, "attach", False):
        raw.setdefault("viewer", {})["attach_only"] = True
    if getattr(args, "manual", None):
        raw.setdefault("manual", {})["mode"] = args.manual
    if getattr(args, "mode", None):
        raw.setdefault("capture", {})["mode"] = args.mode
    if getattr(args, "background", False):
        raw.setdefault("capture", {})["background"] = True
    if getattr(args, "lang", None):
        raw.setdefault("ocr", {})["lang"] = args.lang
    if getattr(args, "workers", None):
        raw.setdefault("ocr", {})["workers"] = args.workers
    return raw


def _config_path(args) -> Path | None:
    """--config if given, else config.yaml next to Capture.bat (the install folder), if present."""
    if getattr(args, "config", None):
        return Path(args.config)
    auto = Path(__file__).resolve().parent.parent / "config.yaml"
    if auto.is_file():
        print(f"Using settings from {auto}")
        return auto
    return None


def _resolve_viewer(raw: dict) -> dict:
    """``viewer.profile: auto`` (the default) -> the reader installed on this machine."""
    v = raw.setdefault("viewer", {})
    if v.get("profile", "auto") != "auto":
        return raw
    from .viewers.profiles import detect_installed_profile
    found = detect_installed_profile()
    if not found:
        raise ConfigError("No supported reader found (Adobe Acrobat / Acrobat Reader / PDF-XChange). Install one, "
                          "or use --viewer generic with viewer.executable set in a config file.")
    v["profile"] = found
    print(f"Reader: {found} (auto-detected)")
    return raw


def _resolved_config(run_dir: Path) -> HarnessConfig:
    """Load the config saved with a run (already merged with its profile)."""
    raw = json.loads((run_dir / "config.resolved.json").read_text(encoding="utf-8"))
    cfg = _build(HarnessConfig, raw, "config")
    validate(cfg)
    return cfg


def _default_run_dir(cfg: HarnessConfig) -> Path:
    name = cfg.output.run_name or f"{Path(cfg.document.path).stem}_{time.strftime('%Y%m%d-%H%M%S')}"
    return Path(cfg.output.root) / name


# --------------------------------------------------------------------------- commands

def cmd_run(args) -> int:
    _require_windows()
    _enable_dpi()
    from .runner import Harness

    raw = _resolve_viewer(_apply_overrides(load_raw(_config_path(args)), args))
    cfg = build_config(raw)
    if not cfg.document.path:
        raise ConfigError("give the document: `run C:\\path\\file.pdf` (or document.path in a config file)")
    run_dir = Path(args.run_dir) if args.run_dir else _default_run_dir(cfg)
    af, sf = windows_factories(cfg)
    if args.pdf_next_to_original:
        cfg.pdf.copy_next_to_original = True
    h = Harness(cfg, run_dir, af, sf, skip_ocr=args.no_ocr, skip_pdf=args.no_pdf, force=args.force)
    code = h.run()
    print(f"\nRun directory: {run_dir}\nReport: {run_dir / 'report.md'}\nExit code: {code}")
    return code


def cmd_resume(args) -> int:
    _require_windows()
    _enable_dpi()
    from .runner import Harness

    run_dir = Path(args.run_dir)
    if args.config:
        cfg = build_config(_resolve_viewer(_apply_overrides(load_raw(Path(args.config)), args)))
    else:
        cfg = _resolved_config(run_dir)
        if args.manual:
            cfg.manual.mode = args.manual
    af, sf = windows_factories(cfg)
    if args.pdf_next_to_original:
        cfg.pdf.copy_next_to_original = True
    h = Harness(cfg, run_dir, af, sf, resume=True, force=args.force, skip_ocr=args.no_ocr,
                skip_pdf=args.no_pdf)
    code = h.run()
    print(f"\nReport: {run_dir / 'report.md'}\nExit code: {code}")
    return code


def _offline_harness(run_dir: Path):
    from .runner import Harness

    cfg = _resolved_config(run_dir)

    def no_adapter(*_a):
        raise HarnessError("viewer not available in offline mode")

    return Harness(cfg, run_dir, no_adapter, no_adapter)


def cmd_ocr(args) -> int:
    run_dir = Path(args.run_dir)
    h = _offline_harness(run_dir)
    if args.lang:
        h.cfg.ocr.lang = args.lang
    code = h.ocr_only(redo=args.redo)
    print(f"OCR text: {run_dir / 'ocr'}\nReport: {run_dir / 'report.md'}")
    return code


def cmd_pdf(args) -> int:
    run_dir = Path(args.run_dir)
    h = _offline_harness(run_dir)
    if args.no_text_layer:
        h.cfg.pdf.text_layer = False
    if args.page_size:
        h.cfg.pdf.page_size = args.page_size
    code = h.pdf_only(explicit_output=Path(args.output) if args.output else None,
                      next_to_original=True if args.next_to_original else None,
                      overwrite=True if args.overwrite else None)
    pdf = h.manifest.data["pdf"]
    print("\n".join(f"Searchable PDF: {f}" for f in pdf["files"]))
    print(f"Pages: {pdf['pages']}  text layer: {pdf['text_layer']}  verified: {pdf['verification']['ok']}")
    for p in pdf["verification"]["problems"]:
        print(f"  problem: {p}")
    return code


def cmd_report(args) -> int:
    from .report import write_report
    from .state import Manifest, RunPaths

    paths = RunPaths(Path(args.run_dir))
    s = write_report(Manifest.load(paths), paths)
    print(paths.report_md.read_text(encoding="utf-8") if args.print else f"Report: {paths.report_md}")
    return 2 if s["pages_needing_review"] else 0


def cmd_list_windows(args) -> int:
    _require_windows()
    _enable_dpi()
    from . import platform_win as pw

    for w in pw.top_level_windows():
        if not w.title and not args.all:
            continue
        exe = os.path.basename(pw.process_image_name(w.pid))
        print(f"hwnd={w.hwnd:<10} pid={w.pid:<6} exe={exe:<22} class={w.class_name:<28} "
              f"rect={w.rect.as_list()} owner={w.owner} title={w.title!r}")
    print("\nMonitors:")
    for m in pw.monitors():
        print(f"  {m.device} rect={m.rect.as_list()} dpi={m.dpi} scale={m.scale} primary={m.primary}")
    print(f"DPI awareness: {pw.current_dpi_awareness()}")
    return 0


def cmd_calibrate(args) -> int:
    """Inspect a reader window and write what the harness sees.

    Writes to <out>/: window.png and document.png (annotated), children.txt
    (Win32 child windows, largest first: candidates for document_pane),
    uia_tree.txt (pywinauto control identifiers), dialogs.json, indicator.json,
    environment.json and, with --test-nav, navigation.json.
    """
    _require_windows()
    _enable_dpi()
    from PIL import ImageDraw

    from . import platform_win as pw
    from .estop import EmergencyStopMonitor
    from .imaging import changed_fraction, crop_to_page
    from .logging_utils import setup_logging
    from .manual import ManualCheckpoint
    from .stability import wait_for_stable

    raw = _resolve_viewer(_apply_overrides(load_raw(_config_path(args)), args))
    cfg = build_config(raw)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    setup_logging(out, cfg.logging.level)
    estop = EmergencyStopMonitor(cfg.safety.estop_hotkey, out / "STOP")
    estop.start()
    checkpoint = ManualCheckpoint(cfg.manual, estop)
    af, sf = windows_factories(cfg)
    adapter = af(checkpoint, estop)
    doc = Path(cfg.document.path) if cfg.document.path else Path("unknown")
    if not cfg.viewer.attach_only and cfg.document.path:
        adapter.launch(doc.resolve())
    adapter.attach(doc, cfg.viewer.launch_timeout_s)
    source = sf(adapter)

    wr, dr = adapter.window_rect(), adapter.document_rect()
    win = source.grab(wr)
    d = ImageDraw.Draw(win)
    d.rectangle([dr.left - wr.left, dr.top - wr.top, dr.right - wr.left - 1, dr.bottom - wr.top - 1],
                outline=(255, 0, 0), width=3)
    docimg = source.grab(dr)
    crop = crop_to_page(docimg, cfg.capture.crop.canvas_tolerance, cfg.capture.crop.padding,
                        cfg.capture.crop.min_page_fraction)
    b = crop.box
    d.rectangle([dr.left - wr.left + b[0], dr.top - wr.top + b[1], dr.left - wr.left + b[2] - 1,
                 dr.top - wr.top + b[3] - 1], outline=(0, 160, 255), width=2)
    win.save(out / "window.png")
    docimg.save(out / "document.png")

    kids = sorted(pw.child_windows(adapter.main_handle()), key=lambda k: -k[3].area)
    with open(out / "children.txt", "w", encoding="utf-8") as f:
        f.write("# Visible Win32 child windows, largest first. Use class/title for viewer.document_pane.\n")
        for h, cls, title, r in kids[:200]:
            f.write(f"hwnd={h:<10} class={cls!r:<32} title={title!r:<30} rect={r.as_list()} area={r.area}\n")
    try:
        from pywinauto import Desktop
        Desktop(backend="uia").window(handle=adapter.main_handle()).print_control_identifiers(
            depth=args.depth, filename=str(out / "uia_tree.txt"))
    except Exception as exc:
        (out / "uia_tree.txt").write_text(f"failed: {exc}\n", encoding="utf-8")
    dialogs = [x.to_dict() for x in adapter.list_dialogs(dr)]
    (out / "dialogs.json").write_text(json.dumps(dialogs, indent=2), encoding="utf-8")
    ind = adapter.read_page_indicator()
    (out / "indicator.json").write_text(json.dumps(ind.__dict__, indent=2), encoding="utf-8")
    env = adapter.environment_info()
    env["capture_protected"] = adapter.capture_protected()
    env["page_crop_box"] = list(crop.box)
    env["page_crop_fallback"] = crop.fallback
    (out / "environment.json").write_text(json.dumps(env, indent=2, default=str), encoding="utf-8")

    if args.test_nav:
        def stable():
            return wait_for_stable(lambda: source.grab(adapter.document_rect()), cfg.capture.stability,
                                   sleep=estop.sleep, is_busy=adapter.is_busy)
        adapter.park_cursor()
        r0 = stable()
        i0 = adapter.read_page_indicator()
        adapter.next_page(0)
        r1 = stable()
        i1 = adapter.read_page_indicator()
        adapter.prev_page()
        r2 = stable()
        i2 = adapter.read_page_indicator()
        nav = {
            "start": {"indicator": i0.__dict__, "stable": r0.stable, "stability_s": r0.elapsed_s},
            "after_next": {"indicator": i1.__dict__, "stable": r1.stable, "stability_s": r1.elapsed_s,
                           "changed_fraction_vs_start": changed_fraction(r0.frame, r1.frame)},
            "after_prev": {"indicator": i2.__dict__, "stable": r2.stable,
                           "changed_fraction_vs_start": changed_fraction(r0.frame, r2.frame)},
        }
        nav["verdict"] = ("ok" if nav["after_next"]["changed_fraction_vs_start"] > cfg.capture.same_page_threshold
                          and nav["after_prev"]["changed_fraction_vs_start"] <= cfg.capture.same_page_threshold
                          else "check keys / focus / page layout")
        (out / "navigation.json").write_text(json.dumps(nav, indent=2, default=str), encoding="utf-8")
        print(f"navigation test: {nav['verdict']}")
    estop.stop()
    source.close()
    print(f"window rect {wr.as_list()}  document rect {dr.as_list()}  pane found: "
          f"{env.get('document_pane_found')}  indicator: {ind.current}/{ind.total} ({ind.source})  "
          f"capture protected: {env['capture_protected']}  dialogs: {len(dialogs)}")
    print(f"Calibration output: {out.resolve()}")
    return 0


def cmd_batch(args) -> int:
    from .batch import Batch, default_output_dir, details_dir, discover, echo_safe

    out = Path(args.out) if args.out else default_output_dir(args.inputs)
    items = discover(args.inputs, recursive=not args.no_recursive, exclude=out)
    if not items:
        print("No PDF files found.")
        return 0
    if args.dry_run:
        for it in items:
            print(f"{it.path}  ->  {out / it.rel_out}.ocr.pdf"
                  + (f"  (known pages {it.known_pages})" if it.known_pages else ""))
        print(f"\n{len(items)} document(s). Output folder: {out}")
        return 0
    _require_windows()
    _enable_dpi()
    raw = _resolve_viewer(_apply_overrides(load_raw(_config_path(args)), args))
    raw.setdefault("document", {}).pop("path", None)
    build_config(deep_merge(raw, {"document": {"path": str(items[0].path)}}))   # validate once up front
    if args.no_pdf:
        raw.setdefault("pdf", {})["enabled"] = False
    def setup_prompt(text: str) -> None:
        print(f"\nBefore the batch starts:\n  {text}\n")
        print("The batch then runs unattended. Emergency stop: Ctrl+Shift+F12, or create a file named STOP in "
              f"{details_dir(out) / '_runs'}.")
        try:
            input("Press Enter to start (Ctrl+C to cancel)... ")
        except EOFError:
            pass

    batch = Batch(raw, items, out, windows_factories, interactive=args.interactive, echo=echo_safe,
                  setup_prompt=None if args.no_prompt else setup_prompt)
    return batch.run()


def cmd_dashboard(args) -> int:
    from .dashboard import serve

    serve(Path(args.folder), args.port, not args.no_browser)
    return 0


def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    raw = load_raw(_config_path(args))
    return run_doctor(raw, Path(args.out_dir) if args.out_dir else Path.cwd())


def cmd_init_config(args) -> int:
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    text = example.read_text(encoding="utf-8") if example.exists() else ""
    if args.viewer:
        text = text.replace("profile: auto", f"profile: {args.viewer}", 1)
    sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="doc_capture_harness",
                                description="Capture rendered pages from a desktop PDF reader and verify them with OCR.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, with_config=True):
        if with_config:
            sp.add_argument("--config", "-c", help="YAML/JSON configuration file")
        sp.add_argument("--document", "-d", help="override document.path")
        sp.add_argument("--viewer", choices=["auto", "acrobat", "pdfxchange", "generic"],
                        help="reader to use (default: auto-detect)")
        sp.add_argument("--known-pages", type=int, help="override document.known_page_count")
        sp.add_argument("--max-pages", type=int, help="override document.max_pages")
        sp.add_argument("--attach", action="store_true", help="attach to an already-open reader window")
        sp.add_argument("--manual", choices=["messagebox", "console", "abort"], help="manual checkpoint mode")
        sp.add_argument("--background", action="store_true",
                        help="keep using the PC: capture the reader window without focus, keys or mouse")
        sp.add_argument("--mode", choices=["fit_page", "scroll"], help="override capture.mode")
        sp.add_argument("--lang", help="override ocr.lang (e.g. eng+deu)")
        sp.add_argument("--workers", type=int, help="parallel OCR processes (default: CPU count - 1)")
        sp.add_argument("--time-limit", type=float, metavar="MIN",
                        help="stop a document after MIN minutes of capture (resumable)")

    r = sub.add_parser("run", help="one document: capture + OCR + searchable PDF + report")
    r.add_argument("doc", nargs="?", help="the document (instead of --document / config)")
    common(r)
    r.add_argument("--run-dir", help="output directory (default: <output.root>/<stem>_<timestamp>)")
    r.add_argument("--no-ocr", action="store_true", help="capture only; run `ocr` later")
    r.add_argument("--force", action="store_true", help="reuse a run directory that already has a manifest")
    r.add_argument("--no-pdf", action="store_true", help="skip building the searchable PDF")
    r.add_argument("--pdf-next-to-original", action="store_true",
                   help="also write <stem>.ocr.pdf next to the original (never over it)")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="continue an interrupted run")
    common(rs)
    rs.add_argument("--run-dir", required=True)
    rs.add_argument("--no-ocr", action="store_true")
    rs.add_argument("--force", action="store_true", help="resume even if the document hash changed")
    rs.add_argument("--no-pdf", action="store_true")
    rs.add_argument("--pdf-next-to-original", action="store_true")
    rs.set_defaults(func=cmd_resume)

    o = sub.add_parser("ocr", help="(re)run OCR on captured pages; works on any OS")
    o.add_argument("--run-dir", required=True)
    o.add_argument("--redo", action="store_true", help="re-OCR pages that already have text")
    o.add_argument("--lang", help="override ocr.lang")
    o.set_defaults(func=cmd_ocr)

    pd = sub.add_parser("pdf", help="(re)build the searchable PDF from captured pages; works on any OS")
    pd.add_argument("--run-dir", required=True)
    pd.add_argument("--output", help="explicit output path (must not be the original document)")
    pd.add_argument("--next-to-original", action="store_true",
                    help="also write <stem>.ocr.pdf in the original document's folder")
    pd.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    pd.add_argument("--no-text-layer", action="store_true", help="image-only PDF (no OCR text)")
    pd.add_argument("--page-size", choices=["original", "letter", "a4", "pixels"])
    pd.set_defaults(func=cmd_pdf)

    rp = sub.add_parser("report", help="rebuild report.json/report.md from the manifest")
    rp.add_argument("--run-dir", required=True)
    rp.add_argument("--print", action="store_true")
    rp.set_defaults(func=cmd_report)

    c = sub.add_parser("calibrate", help="inspect the reader window and test navigation")
    common(c)
    c.add_argument("--out", default="calibration")
    c.add_argument("--depth", type=int, default=6, help="UIA tree depth to dump")
    c.add_argument("--test-nav", action="store_true", help="press next then prev and report what changed")
    c.set_defaults(func=cmd_calibrate)

    b = sub.add_parser("batch", help="many documents: a folder, a .csv/.txt list, or several files")
    b.add_argument("inputs", nargs="+", help="folder(s), PDF file(s), or a .csv (path,known_pages,lang) / .txt list")
    b.add_argument("--out", help="output folder (default: <input folder>\\ocr_output)")
    b.add_argument("--config", "-c", help="optional YAML/JSON configuration applied to every document")
    b.add_argument("--viewer", choices=["auto", "acrobat", "pdfxchange", "generic"], help="default: auto-detect")
    b.add_argument("--interactive", action="store_true",
                   help="pause for the operator on problems (default: skip the document and continue)")
    b.add_argument("--lang", help="OCR language(s), e.g. eng+deu")
    b.add_argument("--workers", type=int, help="parallel OCR processes")
    b.add_argument("--time-limit", type=float, metavar="MIN", help="per-document capture time limit")
    b.add_argument("--max-pages", type=int, help="per-document page cap")
    b.add_argument("--no-pdf", action="store_true", help="text only, no searchable PDF")
    b.add_argument("--no-recursive", action="store_true", help="do not look in sub-folders")
    b.add_argument("--dry-run", action="store_true", help="list what would be processed, then exit")
    b.add_argument("--no-prompt", action="store_true",
                   help="do not wait for Enter before starting (scheduled/remote runs)")
    b.set_defaults(func=cmd_batch)

    db = sub.add_parser("dashboard", help="open a live progress page in the browser (run in a second window)")
    db.add_argument("folder", help="a run directory, or an output folder holding several runs")
    db.add_argument("--port", type=int, default=8765)
    db.add_argument("--no-browser", action="store_true", help="only print the URL")
    db.set_defaults(func=cmd_dashboard)

    dr = sub.add_parser("doctor", help="check that this machine is ready (Python packages, Tesseract, reader, display)")
    dr.add_argument("--config", "-c")
    dr.add_argument("--out-dir", help="where output will be written (checks free space)")
    dr.set_defaults(func=cmd_doctor)

    lw = sub.add_parser("list-windows", help="list top-level windows and monitors")
    lw.add_argument("--all", action="store_true", help="include untitled windows")
    lw.set_defaults(func=cmd_list_windows)

    ic = sub.add_parser("init-config", help="print the example configuration")
    ic.add_argument("--viewer", choices=["acrobat", "pdfxchange", "generic"])
    ic.set_defaults(func=cmd_init_config)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (ConfigError, PlatformUnsupported, HarnessError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("interrupted (run can be resumed with `resume --run-dir ...`)", file=sys.stderr)
        return 3
