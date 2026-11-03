"""Command-line interface.

    python -m doc_capture_harness run       --config cfg.yaml [options]
    python -m doc_capture_harness resume    --run-dir runs/<run>
    python -m doc_capture_harness ocr       --run-dir runs/<run> [--redo]
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
from .config import HarnessConfig, _build, build_config, load_raw, validate
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
    if getattr(args, "document", None):
        doc["path"] = args.document
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
    if getattr(args, "lang", None):
        raw.setdefault("ocr", {})["lang"] = args.lang
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

    raw = _apply_overrides(load_raw(Path(args.config)) if args.config else {}, args)
    cfg = build_config(raw)
    if not cfg.document.path:
        raise ConfigError("document.path (or --document) is required")
    run_dir = Path(args.run_dir) if args.run_dir else _default_run_dir(cfg)
    af, sf = windows_factories(cfg)
    h = Harness(cfg, run_dir, af, sf, skip_ocr=args.no_ocr, force=args.force)
    code = h.run()
    print(f"\nRun directory: {run_dir}\nReport: {run_dir / 'report.md'}\nExit code: {code}")
    return code


def cmd_resume(args) -> int:
    _require_windows()
    _enable_dpi()
    from .runner import Harness

    run_dir = Path(args.run_dir)
    if args.config:
        cfg = build_config(_apply_overrides(load_raw(Path(args.config)), args))
    else:
        cfg = _resolved_config(run_dir)
        if args.manual:
            cfg.manual.mode = args.manual
    af, sf = windows_factories(cfg)
    h = Harness(cfg, run_dir, af, sf, resume=True, force=args.force, skip_ocr=args.no_ocr)
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

    raw = _apply_overrides(load_raw(Path(args.config)) if args.config else {}, args)
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


def cmd_init_config(args) -> int:
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    text = example.read_text(encoding="utf-8") if example.exists() else ""
    if args.viewer:
        text = text.replace("profile: acrobat", f"profile: {args.viewer}", 1)
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
        sp.add_argument("--viewer", choices=["acrobat", "pdfxchange", "generic"], help="override viewer.profile")
        sp.add_argument("--known-pages", type=int, help="override document.known_page_count")
        sp.add_argument("--max-pages", type=int, help="override document.max_pages")
        sp.add_argument("--attach", action="store_true", help="attach to an already-open reader window")
        sp.add_argument("--manual", choices=["messagebox", "console", "abort"], help="manual checkpoint mode")
        sp.add_argument("--mode", choices=["fit_page", "scroll"], help="override capture.mode")
        sp.add_argument("--lang", help="override ocr.lang (e.g. eng+deu)")

    r = sub.add_parser("run", help="capture + OCR + report")
    common(r)
    r.add_argument("--run-dir", help="output directory (default: <output.root>/<stem>_<timestamp>)")
    r.add_argument("--no-ocr", action="store_true", help="capture only; run `ocr` later")
    r.add_argument("--force", action="store_true", help="reuse a run directory that already has a manifest")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="continue an interrupted run")
    common(rs)
    rs.add_argument("--run-dir", required=True)
    rs.add_argument("--no-ocr", action="store_true")
    rs.add_argument("--force", action="store_true", help="resume even if the document hash changed")
    rs.set_defaults(func=cmd_resume)

    o = sub.add_parser("ocr", help="(re)run OCR on captured pages; works on any OS")
    o.add_argument("--run-dir", required=True)
    o.add_argument("--redo", action="store_true", help="re-OCR pages that already have text")
    o.add_argument("--lang", help="override ocr.lang")
    o.set_defaults(func=cmd_ocr)

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
