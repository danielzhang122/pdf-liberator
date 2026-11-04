import itertools

import pytest
from PIL import Image

from doc_capture_harness.config import build_config, StabilityConfig, PromptsConfig, PromptRule
from doc_capture_harness.errors import ConfigError, UserAbort
from doc_capture_harness.estop import EmergencyStopMonitor
from doc_capture_harness.imaging import (changed_fraction, crop_to_page, find_overlap, stitch_vertical,
                                         dhash, hamming, is_all_black)
from doc_capture_harness.inputs import pywinauto_to_pyautogui
from doc_capture_harness.models import DialogInfo, Rect
from doc_capture_harness.prompts import PromptHandler
from doc_capture_harness.stability import wait_for_stable
from fakes import make_page, CANVAS


# ----------------------------------------------------------------- config

def test_unknown_key_rejected(doc):
    with pytest.raises(ConfigError, match="unknown key"):
        build_config({"document": {"path": str(doc), "max_pagez": 3}})


def test_profile_merge_and_override():
    cfg = build_config({"viewer": {"profile": "acrobat", "keys": {"next_page": "{PGDN}"}}})
    assert cfg.viewer.keys.next_page == "{PGDN}"
    assert cfg.viewer.keys.goto_page == "^+n"          # from profile
    assert cfg.viewer.document_pane.class_name == "AVL_AVView"
    assert any(r.name == "javascript-prompt-requires-operator" for r in cfg.prompts.rules)


def test_unknown_prompts_cannot_be_auto_dismissed():
    with pytest.raises(ConfigError):
        build_config({"prompts": {"unknown_action": "click"}})


# ----------------------------------------------------------------- imaging

def _viewport_with_page(page):
    vp = Image.new("RGB", (800, 900), CANVAS)
    vp.paste(page, (100, 60))
    return vp


def test_crop_to_page_finds_page():
    page = make_page(1)
    res = crop_to_page(_viewport_with_page(page), padding=0)
    assert not res.fallback
    l, t, r, b = res.box
    assert abs(l - 100) <= 1 and abs(t - 60) <= 1
    assert abs((r - l) - page.width) <= 2 and abs((b - t) - page.height) <= 2


def test_crop_fallback_when_page_fills_viewport():
    page = make_page(1, 800, 900)
    res = crop_to_page(page)
    assert res.fallback


def test_stitch_reconstructs_tall_page():
    tall = make_page(1, 600, 2000, lines=40)
    vh, step = 700, 450
    slices = [tall.crop((0, y, 600, min(2000, y + vh))) for y in range(0, 2000 - vh + 1, step)]
    slices.append(tall.crop((0, 2000 - vh, 600, 2000)))
    out, matches = stitch_vertical(slices)
    assert all(m.accepted for m in matches)
    assert out.size == tall.size
    assert changed_fraction(out, tall, downscale=1) == 0.0


def test_overlap_rejects_unrelated_slices():
    a = make_page(1, 600, 700)
    b = make_page(9, 600, 700, label="Different")
    b = b.transpose(Image.FLIP_TOP_BOTTOM)
    m = find_overlap(a, b)
    assert not m.accepted or m.mean_diff > 5


def test_dhash_detects_duplicates():
    a, b, c = make_page(1), make_page(1), make_page(2)
    assert hamming(dhash(a), dhash(b)) == 0
    assert hamming(dhash(a), dhash(c)) > 3


def test_all_black():
    assert is_all_black(Image.new("RGB", (100, 100)))
    assert not is_all_black(make_page(1))


def test_key_translation():
    assert pywinauto_to_pyautogui("^+n") == [(["ctrl", "shift"], "n", 1)]
    assert pywinauto_to_pyautogui("{PGDN 3}") == [([], "pagedown", 3)]
    assert pywinauto_to_pyautogui("12{ENTER}") == [([], "1", 1), ([], "2", 1), ([], "enter", 1)]


# ----------------------------------------------------------------- stability

def test_stability_waits_for_render():
    frames = [Image.new("L", (100, 100), v) for v in (0, 50, 100)] + [Image.new("L", (100, 100), 200)] * 10
    it = iter(frames)
    cfg = StabilityConfig(interval_s=0, min_wait_s=0, required_stable_frames=3, timeout_s=5)
    res = wait_for_stable(lambda: next(it), cfg, sleep=lambda s: None)
    assert res.stable and res.frames == 6


def test_stability_times_out_when_animating():
    c = itertools.count()
    cfg = StabilityConfig(interval_s=0, min_wait_s=0, required_stable_frames=3, timeout_s=0.2)
    res = wait_for_stable(lambda: Image.new("L", (50, 50), next(c) * 20 % 255), cfg, sleep=lambda s: None)
    assert not res.stable


def test_stability_respects_busy():
    cfg = StabilityConfig(interval_s=0, min_wait_s=0, required_stable_frames=3, timeout_s=0.2)
    res = wait_for_stable(lambda: Image.new("L", (50, 50), 9), cfg, sleep=lambda s: None, is_busy=lambda: True)
    assert not res.stable and res.busy_seen


# ----------------------------------------------------------------- prompts

class _DlgAdapter:
    def __init__(self, dialogs):
        self.dialogs = dialogs
        self.clicked = []

    def list_dialogs(self, _r):
        return list(self.dialogs)

    def click_dialog_button(self, d, b):
        self.clicked.append((d.title, b))
        self.dialogs.remove(d)
        return True

    def send_dialog_keys(self, d, k):
        return False


def _dlg(title, text, buttons=("OK", "Cancel"), owned=True, h=1):
    return DialogInfo(h, title, "#32770", 1, Rect(0, 0, 100, 100), [text], list(buttons), owned)


def _handler(adapter, rules, answers, obstruction_wait_s=0.0):
    calls = []

    def checkpoint(reason, msg):
        calls.append(reason)
        a = answers.pop(0)
        if a == "abort":
            raise UserAbort(reason)
        if a == "dismiss":
            adapter.dialogs.clear()
            return "retry"
        return a

    cfg = PromptsConfig(rules=[PromptRule(**r) for r in rules], obstruction_wait_s=obstruction_wait_s)
    return PromptHandler(cfg, adapter, checkpoint, EmergencyStopMonitor(None, None)), calls


def test_rule_clicks_known_benign_dialog():
    ad = _DlgAdapter([_dlg("Adobe Acrobat", "Reading untagged document")])
    h, calls = _handler(ad, [{"name": "untagged", "text_re": "untagged", "action": "click", "button": "Cancel"}], [])
    h.handle(None)
    assert ad.clicked == [("Adobe Acrobat", "Cancel")] and calls == []


def test_security_dialog_needs_acknowledgement():
    ad = _DlgAdapter([_dlg("Security Warning", "Allow this document to connect?", ["Allow", "Block"])])
    h, calls = _handler(ad, [{"name": "w", "title_re": "Warning", "action": "click", "button": "Block"}],
                        ["dismiss"])
    h.handle(None)
    assert calls == ["dialog"] and ad.clicked == []


def test_security_dialog_with_explicit_acknowledgement():
    ad = _DlgAdapter([_dlg("Security Warning", "Allow this document to connect?", ["Allow", "Block"])])
    h, calls = _handler(ad, [{"name": "w", "title_re": "Warning", "action": "click", "button": "Block",
                              "acknowledge_security": True}], [])
    h.handle(None)
    assert ad.clicked == [("Security Warning", "Block")]


def test_unknown_dialog_goes_to_manual_and_continue_is_remembered():
    ad = _DlgAdapter([_dlg("Something new", "Hello")])
    h, calls = _handler(ad, [], ["continue"])
    h.handle(None)
    h.handle(None)        # accepted handle is not asked about again
    assert calls == ["dialog"]


def test_obstruction_triggers_checkpoint():
    ad = _DlgAdapter([_dlg("Chat", "new message", owned=False)])
    h, calls = _handler(ad, [], ["dismiss"])
    h.handle(Rect(0, 0, 10, 10))
    assert calls == ["obstruction"]


def test_rule_max_times():
    ad = _DlgAdapter([])
    rule = {"name": "r", "text_re": "nag", "action": "click", "button": "OK", "max_times": 2}
    h, calls = _handler(ad, [rule], ["dismiss"])
    for i in range(3):
        ad.dialogs.append(_dlg("Nag", "nag screen", h=i + 10))
        h.handle(None)
    assert len(ad.clicked) == 2 and calls == ["dialog"]


def test_estop_file(tmp_path):
    stop = tmp_path / "STOP"
    m = EmergencyStopMonitor(None, stop)
    m.check()
    stop.write_text("x")
    from doc_capture_harness.errors import EmergencyStop
    with pytest.raises(EmergencyStop):
        m.check()


def test_example_config_is_valid():
    from pathlib import Path
    from doc_capture_harness.config import load_config, load_raw
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.yaml")
    assert cfg.viewer.profile in ("acrobat", "pdfxchange", "generic")   # "auto" resolved
    names = [r.name for r in cfg.prompts.rules]
    assert names[0] == "untagged-document-notice"          # user rules first
    raw = load_raw(Path(__file__).resolve().parent.parent / "config.example.yaml")
    raw["viewer"]["profile"] = "acrobat"
    names = [r.name for r in build_config(raw).prompts.rules]
    assert "javascript-prompt-requires-operator" in names  # profile rules appended


class _VanishingAdapter(_DlgAdapter):
    """A foreign pop-up that closes by itself after a few checks."""

    def __init__(self, dialogs, vanish_after):
        super().__init__(dialogs)
        self.calls = 0
        self.vanish_after = vanish_after

    def list_dialogs(self, _r):
        self.calls += 1
        if self.calls > self.vanish_after:
            self.dialogs.clear()
        return list(self.dialogs)


def test_transient_obstruction_is_waited_out(monkeypatch):
    ad = _VanishingAdapter([_dlg("NotifyView", "Document opened", ["Open"], owned=False)], vanish_after=3)
    h, calls = _handler(ad, [], [], obstruction_wait_s=30)
    monkeypatch.setattr(h.estop, "sleep", lambda s: None)
    h.handle(Rect(0, 0, 10, 10))
    assert calls == [] and ad.calls == 4
    assert any(r["action"] == "obstruction_wait" for r in h.stats.handled)


def test_persistent_obstruction_still_checkpoints_after_wait(monkeypatch):
    ad = _DlgAdapter([_dlg("NotifyView", "Document opened", ["Open"], owned=False)])
    h, calls = _handler(ad, [], ["dismiss"], obstruction_wait_s=0.05)
    h.handle(Rect(0, 0, 10, 10))
    assert calls == ["obstruction"]


def test_rules_for_other_apps_need_scope(monkeypatch):
    rule = {"name": "notify", "class_re": "#32770", "title_re": "^NotifyView$", "action": "click",
            "button": "Open"}
    ad = _DlgAdapter([_dlg("NotifyView", "Document opened", ["Open"], owned=False)])
    h, calls = _handler(ad, [rule], ["dismiss"])             # default scope: viewer -> not applied
    h.handle(Rect(0, 0, 10, 10))
    assert ad.clicked == [] and calls == ["obstruction"]
    ad = _DlgAdapter([_dlg("NotifyView", "Document opened", ["Open"], owned=False)])
    h, calls = _handler(ad, [{**rule, "scope": "other"}], [])
    h.handle(Rect(0, 0, 10, 10))
    assert ad.clicked == [("NotifyView", "Open")] and calls == []


def test_commented_out_config_sections_load():
    cfg = build_config({"prompts": {"rules": None}, "ocr": None})
    assert cfg.prompts.rules == [] and cfg.ocr.lang == "eng"


def test_crop_ignores_floating_toolbar_on_canvas():
    page = make_page(1)
    vp = _viewport_with_page(page)                       # page at (100, 60)
    from PIL import ImageDraw
    ImageDraw.Draw(vp).rectangle([8, 200, 48, 520], fill=(245, 245, 245))   # tool strip left of the page
    res = crop_to_page(vp, padding=0)
    l, t, r, b = res.box
    assert abs(l - 100) <= 1 and abs(t - 60) <= 1 and abs((r - l) - page.width) <= 2


def test_acrobat_profile_ignores_floating_toolbars():
    cfg = build_config({"viewer": {"profile": "acrobat"}})
    tb = DialogInfo(7, "", "AVL_AVPopup", 1, Rect(0, 0, 50, 300), [], ["Select text", "Add a comment"], True)
    ad = _DlgAdapter([tb])
    h = PromptHandler(cfg.prompts, ad, lambda r, m: (_ for _ in ()).throw(AssertionError(r)),
                      EmergencyStopMonitor(None, None))
    h.handle(None)
    assert h.stats.handled[0]["action"] == "ignore"
    titled = DialogInfo(8, "Something", "AVL_AVPopup", 1, Rect(0, 0, 50, 300), ["?"], ["OK"], True)
    from doc_capture_harness.prompts import rule_matches
    assert not any(rule_matches(r, titled) for r in cfg.prompts.rules if r.name == "acrobat-floating-toolbars")


@pytest.mark.parametrize("grey", [229, 240, 246])
def test_light_grey_canvas_is_still_detected(grey):
    from doc_capture_harness.imaging import layout_issues
    page = make_page(1)
    vp = Image.new("RGB", (800, 900), (grey, grey, grey))
    vp.paste(page, (100, 60))
    res = crop_to_page(vp, padding=0)
    assert not res.fallback and abs(res.box[0] - 100) <= 1 and abs(res.box[1] - 60) <= 1
    assert layout_issues(vp) == []


def test_white_canvas_does_not_block():
    from doc_capture_harness.imaging import layout_issues
    vp = Image.new("RGB", (800, 900), "white")
    vp.paste(make_page(1), (100, 60))
    assert layout_issues(vp) == [] and crop_to_page(vp).fallback
