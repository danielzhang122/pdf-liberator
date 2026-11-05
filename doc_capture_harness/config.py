"""Configuration model and loader.

The configuration is a YAML (or JSON) file mapped onto nested dataclasses.
Unknown keys are rejected so a typo cannot silently change behaviour.
Viewer *profiles* (``acrobat``, ``pdfxchange``, ``generic``) supply defaults
for the ``viewer`` section; anything in the user's file overrides them.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .errors import ConfigError


# --------------------------------------------------------------------------- viewer

@dataclass
class PaneSelector:
    """How to find the child window that renders document pages.

    Matching is done against Win32 child windows (class name / window text) of
    the viewer's main window. All given fields must match. Use the
    ``calibrate`` command to discover suitable values for your reader version.
    """
    class_name: Optional[str] = None
    class_name_re: Optional[str] = None
    title: Optional[str] = None
    title_re: Optional[str] = None
    pick: str = "largest"          # largest | first


@dataclass
class ViewerKeys:
    """Keystrokes in pywinauto ``send_keys`` syntax (^=Ctrl, +=Shift, %=Alt)."""
    next_page: str = "{RIGHT}"
    prev_page: str = "{LEFT}"
    first_page: str = "{HOME}"
    last_page: Optional[str] = "{END}"
    goto_page: Optional[str] = None              # opens a "go to page" dialog
    scroll_down: Optional[str] = None            # None => mouse wheel
    scroll_up: Optional[str] = None
    alt_next_page: list[str] = field(default_factory=list)   # tried on retries


@dataclass
class IndicatorConfig:
    enabled: bool = True
    # Regexes tried in order against texts of the viewer's UI controls. Each
    # must contain a 'current' group and may contain a 'total' group.
    patterns: list[str] = field(default_factory=lambda: [
        r"(?P<current>\d+)\s*(?:/|of)\s*(?P<total>\d+)",
        r"^\s*(?P<current>\d+)\s*$",
    ])
    total_patterns: list[str] = field(default_factory=lambda: [
        r"(?:/|of)\s*(?P<total>\d+)\s*\)?\s*$",
    ])
    control_types: list[str] = field(default_factory=lambda: ["Edit", "Text", "ComboBox"])
    control_name_re: Optional[str] = None       # narrow the UIA search
    # Fallback: OCR a region given relative to the main window [x, y, w, h].
    ocr_region: Optional[list[int]] = None
    max_uia_controls: int = 400


@dataclass
class BusyConfig:
    title_re: Optional[str] = r"(?i)\b(loading|please wait|not responding)\b"
    check_cursor: bool = True       # IDC_WAIT / IDC_APPSTARTING => busy
    check_hung: bool = True         # IsHungAppWindow


@dataclass
class ViewerConfig:
    profile: str = "generic"           # the CLI resolves "auto" to the installed reader
    executable: Optional[str] = None
    executable_candidates: list[str] = field(default_factory=list)
    launch_args: list[str] = field(default_factory=list)   # "{document}" is substituted
    attach_only: bool = False      # do not launch, attach to an already-open window
    process_names: list[str] = field(default_factory=list)
    window_title_re: Optional[str] = None        # "{stem}" / "{name}" substituted
    window_class_re: Optional[str] = None
    launch_timeout_s: float = 60.0
    document_pane: PaneSelector = field(default_factory=PaneSelector)
    # Fallback when the pane is not found: client area inset by [l, t, r, b].
    viewport_margins: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    keys: ViewerKeys = field(default_factory=ViewerKeys)
    prepare_keys: list[str] = field(default_factory=list)  # e.g. fit-page zoom
    goto_method: str = "sequential"              # dialog | page_field | sequential | manual
    # How to move to the next page when the page count is known: "goto" enters the page
    # number in the go-to-page dialog (immune to lost, delayed or doubled key presses);
    # "keys" presses keys.next_page. Without a known page count, keys are always used.
    navigate_by: str = "keys"
    goto_dialog_title_re: Optional[str] = None
    page_indicator: IndicatorConfig = field(default_factory=IndicatorConfig)
    busy: BusyConfig = field(default_factory=BusyConfig)
    focus_pane_by_click: bool = False   # clicking can trigger links/forms; off by default
    # Message shown at a manual checkpoint after the document opens, for setup
    # that cannot be automated reliably (page layout, closing banners, ...).
    setup_checkpoint: Optional[str] = None
    # Exact UI Automation names of reader elements that, when shown over the
    # document area, hide page content; checked before page 1.
    overlay_names: list[str] = field(default_factory=list)
    close_on_finish: bool = False


# --------------------------------------------------------------------------- capture

@dataclass
class StabilityConfig:
    interval_s: float = 0.25
    required_stable_frames: int = 3
    pixel_threshold: int = 10              # per-pixel grey difference that counts as change
    changed_fraction_threshold: float = 0.0008
    min_wait_s: float = 0.3
    timeout_s: float = 10.0
    downscale: int = 2


@dataclass
class ScrollConfig:
    method: str = "wheel"            # wheel | keys
    wheel_notches: int = 5
    max_slices: int = 40
    template_rows: int = 48
    max_mean_diff: float = 8.0       # stitch acceptance (0-255 grey)
    min_template_std: float = 4.0    # below this the overlap is ambiguous


@dataclass
class CropConfig:
    auto_page: bool = True
    canvas_tolerance: int = 14
    padding: int = 4
    min_page_fraction: float = 0.15   # page bbox smaller than this => fallback


@dataclass
class CaptureConfig:
    method: str = "screen"           # screen | printwindow
    mode: str = "fit_page"           # fit_page | scroll
    stability: StabilityConfig = field(default_factory=StabilityConfig)
    scroll: ScrollConfig = field(default_factory=ScrollConfig)
    crop: CropConfig = field(default_factory=CropConfig)
    retries: int = 2
    nav_retries: int = 2
    same_page_threshold: float = 0.0015
    blank_std_threshold: float = 2.0
    placeholder_text_re: list[str] = field(default_factory=list)
    placeholder_wait_s: float = 5.0
    end_probe: bool = True
    # Fit-page sanity check: pause if the view shows more than one page
    # (continuous layout) or a page cut off at the top/bottom (zoom too high).
    layout_check: bool = True
    duplicate_hash_distance: int = 3


# --------------------------------------------------------------------------- ocr

@dataclass
class OcrConfig:
    enabled: bool = True
    tesseract_cmd: Optional[str] = None
    lang: str = "eng"
    psm: int = 3
    oem: int = 1
    extra_config: str = ""
    upscale: float = 1.0
    auto_upscale_min_word_height: int = 18   # retry with 2x upscale when text is smaller
    low_word_conf: float = 60.0
    page_min_mean_conf: float = 70.0
    page_max_low_conf_ratio: float = 0.25
    min_word_height_px: int = 10
    save_tsv: bool = True
    workers: int = 0                  # parallel Tesseract processes; 0 = CPU count - 1 (max 8)
    combined_copy_path: Optional[str] = None   # also write combined text here (batch output)


# --------------------------------------------------------------------------- pdf

@dataclass
class PdfConfig:
    """Searchable PDF assembled from the captures. The original is never modified."""
    enabled: bool = True
    text_layer: bool = True              # invisible OCR text over each page image
    output_name: Optional[str] = None    # default "<stem>.ocr.pdf"
    copy_next_to_original: bool = False  # also write <original dir>/<output_name>
    overwrite: bool = False              # replace an existing file outside the run directory
    page_size: str = "original"          # original | letter | a4 | pixels
    fallback_page_size: str = "letter"   # when the original's sizes are unreadable/encrypted
    pixels_dpi: float = 150.0            # page_size: pixels
    image_format: str = "png"            # png (lossless) | jpeg (smaller)
    jpeg_quality: int = 85
    missing_pages: str = "placeholder"   # placeholder | skip
    copy_metadata: bool = True           # title/author/... from the original, if readable
    output_path: Optional[str] = None    # explicit output file (batch mode sets this)
    keep_page_pdfs: bool = False         # keep ocr/pdf/page_NNNN.pdf after merging (faster rebuilds)
    verify_sample_pages: int = 60        # text-layer check on this many pages (all, if fewer)


# --------------------------------------------------------------------------- prompts

DEFAULT_SECURITY_RE = [
    r"(?i)security", r"(?i)\btrust", r"(?i)certificate", r"(?i)password",
    r"(?i)javascript", r"(?i)permission", r"(?i)\ballow\b", r"(?i)\bblock",
    r"(?i)protected view", r"(?i)sign[ -]?in", r"(?i)licen[cs]e",
]


@dataclass
class PromptRule:
    name: str = ""
    title_re: Optional[str] = None
    text_re: Optional[str] = None
    class_re: Optional[str] = None
    # click | keys | wait | manual | abort | ignore
    action: str = "manual"
    button: Optional[str] = None
    keys: Optional[str] = None
    wait_s: float = 2.0
    max_times: int = 5
    # Required to auto-click/keys on a dialog whose text looks security-related.
    acknowledge_security: bool = False
    # Which windows the rule applies to: the reader's own dialogs ("viewer"),
    # other applications' windows covering the page ("other"), or both ("any").
    # Note: "ignore" on another app's window means it will appear in captures.
    scope: str = "viewer"


@dataclass
class PromptsConfig:
    # What to do with a dialog no rule knows: manual (ask) | abort (set the document
    # aside) | ignore (leave it open, never click it, keep capturing; pages it covers
    # are flagged for review). Unknown dialogs are never dismissed.
    unknown_action: str = "manual"
    obstruction_action: str = "manual"  # another program's window over the page: manual | abort | ignore
    obstruction_wait_s: float = 30.0    # let transient pop-ups from other apps disappear first
    security_text_re: list[str] = field(default_factory=lambda: list(DEFAULT_SECURITY_RE))
    rules: list[PromptRule] = field(default_factory=list)
    startup_timeout_s: float = 60.0
    ignore_title_re: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- misc

@dataclass
class ManualConfig:
    mode: str = "messagebox"   # messagebox | console | abort
    beep: bool = True


@dataclass
class SafetyConfig:
    estop_hotkey: Optional[str] = "ctrl+shift+f12"
    stop_file_name: str = "STOP"
    pyautogui_failsafe: bool = True
    allow_pyautogui_fallback: bool = True
    pause_on_focus_loss: bool = True
    refocus_attempts: int = 3


@dataclass
class DocumentConfig:
    path: str = ""
    open_copy: bool = True
    known_page_count: Optional[int] = None
    max_pages: int = 5000
    start_page: int = 1
    time_limit_minutes: Optional[float] = None   # abort (resumable) if capture takes longer


@dataclass
class OutputConfig:
    root: str = "runs"
    run_name: Optional[str] = None
    diagnostics_full_screen: bool = False
    png_compress_level: int = 6
    save_raw_slices: bool = False


@dataclass
class LoggingConfig:
    level: str = "INFO"
    console: bool = True


@dataclass
class HarnessConfig:
    document: DocumentConfig = field(default_factory=DocumentConfig)
    viewer: ViewerConfig = field(default_factory=ViewerConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    pdf: PdfConfig = field(default_factory=PdfConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    manual: ManualConfig = field(default_factory=ManualConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def fingerprint(self) -> str:
        """Hash of capture-relevant settings, used to warn on resume."""
        d = self.to_dict()
        relevant = {k: d[k] for k in ("viewer", "capture")}
        return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- loading

def _build(cls, data: Any, path: str):
    if not dataclasses.is_dataclass(cls):
        return data
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping, got {type(data).__name__}")
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {sorted(unknown)}; allowed: {sorted(names)}")
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = _convert(hints[f.name], data[f.name], f"{path}.{f.name}")
    return cls(**kwargs)


def _convert(tp, value, path):
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Union:  # Optional[X]
        if value is None:
            return None
        inner = [a for a in args if a is not type(None)]
        return _convert(inner[0], value, path)
    if origin is list:
        if value is None:            # an empty YAML section (e.g. everything commented out)
            return []
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list")
        return [_convert(args[0], v, f"{path}[{i}]") for i, v in enumerate(value)]
    if dataclasses.is_dataclass(tp):
        return _build(tp, value, path)
    if tp is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if tp in (int, float, str, bool) and not isinstance(value, tp):
        raise ConfigError(f"{path}: expected {tp.__name__}, got {value!r}")
    return value


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_raw(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    text = Path(path).read_text(encoding="utf-8")
    if str(path).lower().endswith(".json"):
        return json.loads(text) or {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("PyYAML is required for YAML configs (pip install pyyaml)") from exc
    return yaml.safe_load(text) or {}


def build_config(raw: dict) -> HarnessConfig:
    from .viewers.profiles import PROFILES

    raw = copy.deepcopy(raw or {})
    viewer_raw = raw.get("viewer") or {}
    profile = viewer_raw.get("profile", "generic")
    if profile == "auto":
        from .viewers.profiles import detect_installed_profile
        profile = detect_installed_profile() or "generic"
    if profile not in PROFILES:
        raise ConfigError(f"viewer.profile: unknown profile {profile!r}; choose from {sorted(PROFILES)}")
    raw["viewer"] = deep_merge(PROFILES[profile], viewer_raw)
    raw["viewer"]["profile"] = profile
    raw["prompts"] = deep_merge({"rules": []}, raw.get("prompts") or {})
    # Profile-supplied prompt rules are appended after user rules (user rules win).
    profile_rules = PROFILES[profile].get("_prompt_rules", [])
    raw["viewer"].pop("_prompt_rules", None)
    raw["prompts"]["rules"] = list(raw["prompts"].get("rules") or []) + list(profile_rules)
    cfg = _build(HarnessConfig, raw, "config")
    validate(cfg)
    return cfg


def load_config(path: Optional[Path]) -> HarnessConfig:
    return build_config(load_raw(path))


def validate(cfg: HarnessConfig) -> None:
    def check(cond, msg):
        if not cond:
            raise ConfigError(msg)

    check(cfg.capture.mode in ("fit_page", "scroll"), "capture.mode must be fit_page or scroll")
    check(cfg.capture.method in ("screen", "printwindow"), "capture.method must be screen or printwindow")
    check(cfg.capture.scroll.method in ("wheel", "keys"), "capture.scroll.method must be wheel or keys")
    check(cfg.viewer.navigate_by in ("keys", "goto"), "viewer.navigate_by must be keys or goto")
    check(cfg.viewer.goto_method in ("dialog", "page_field", "sequential", "manual"),
          "viewer.goto_method must be dialog, page_field, sequential or manual")
    check(cfg.prompts.unknown_action in ("manual", "abort", "ignore"),
          "prompts.unknown_action must be manual, abort or ignore (unknown prompts are never auto-dismissed)")
    check(cfg.prompts.obstruction_action in ("manual", "abort", "ignore"),
          "prompts.obstruction_action must be manual, abort or ignore")
    check(cfg.manual.mode in ("messagebox", "console", "abort"), "manual.mode must be messagebox, console or abort")
    check(cfg.document.max_pages >= 1, "document.max_pages must be >= 1")
    check(cfg.document.start_page >= 1, "document.start_page must be >= 1")
    if cfg.document.known_page_count is not None:
        check(cfg.document.known_page_count >= 1, "document.known_page_count must be >= 1")
    check(len(cfg.viewer.viewport_margins) == 4, "viewer.viewport_margins must be [left, top, right, bottom]")
    if cfg.viewer.page_indicator.ocr_region is not None:
        check(len(cfg.viewer.page_indicator.ocr_region) == 4, "page_indicator.ocr_region must be [x, y, w, h]")
    for i, r in enumerate(cfg.prompts.rules):
        where = f"prompts.rules[{i}] ({r.name or 'unnamed'})"
        check(r.action in ("click", "keys", "wait", "manual", "abort", "ignore"), f"{where}: bad action {r.action!r}")
        check(any([r.title_re, r.text_re, r.class_re]), f"{where}: needs at least one of title_re/text_re/class_re")
        check(r.action != "click" or r.button, f"{where}: action 'click' requires 'button'")
        check(r.action != "keys" or r.keys, f"{where}: action 'keys' requires 'keys'")
        check(r.scope in ("viewer", "other", "any"), f"{where}: scope must be viewer, other or any")
    check(cfg.pdf.page_size in ("original", "letter", "a4", "pixels"),
          "pdf.page_size must be original, letter, a4 or pixels")
    check(cfg.pdf.fallback_page_size in ("letter", "a4"), "pdf.fallback_page_size must be letter or a4")
    check(cfg.pdf.image_format in ("png", "jpeg"), "pdf.image_format must be png or jpeg")
    check(cfg.pdf.missing_pages in ("placeholder", "skip"), "pdf.missing_pages must be placeholder or skip")
    if cfg.pdf.output_name:
        check(cfg.pdf.output_name.lower().endswith(".pdf") and "/" not in cfg.pdf.output_name
              and "\\" not in cfg.pdf.output_name, "pdf.output_name must be a file name ending in .pdf")
    check(cfg.capture.stability.required_stable_frames >= 2, "stability.required_stable_frames must be >= 2")
