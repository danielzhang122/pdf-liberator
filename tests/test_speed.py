"""Per-page speed: expensive UI lookups are cached, waits are bounded."""
import json
import sys
import time

import pytest
from PIL import Image, ImageDraw

from conftest import fast_config
from doc_capture_harness.config import StabilityConfig, build_config
from doc_capture_harness.lookup_cache import ScanCache
from doc_capture_harness.runner import EXIT_OK, Harness
from doc_capture_harness.stability import wait_for_stable
from fakes import FakeSource, FakeViewer, Scenario, make_page


def test_scan_cache_reuses_found_value():
    calls = []
    c = ScanCache(lambda: calls.append(1) or ["edit"], retry_after_s=60, clock=lambda: 0.0)
    assert [c.get() for _ in range(50)] == [["edit"]] * 50
    assert len(calls) == 1


def test_scan_cache_does_not_rescan_on_every_miss():
    """The old code searched Acrobat's whole UI again on every read when nothing was found."""
    now = [0.0]
    calls = []
    c = ScanCache(lambda: calls.append(1) or [], retry_after_s=60, clock=lambda: now[0])
    for i in range(100):                 # 100 reads within 50 s: one search
        now[0] = i * 0.5
        assert not c.get()
    assert len(calls) == 1
    now[0] = 61.0
    c.get()
    assert len(calls) == 2               # retried once after a minute


def test_scan_cache_stale_and_reset():
    now = [0.0]
    calls = []
    c = ScanCache(lambda: calls.append(1) or ["x"], retry_after_s=60, clock=lambda: now[0])
    c.get()
    c.mark_stale()
    c.get()
    assert len(calls) == 1               # stale: waits for the retry interval
    c.reset()
    c.get()
    assert len(calls) == 2               # reset (new window): rescans at once


def test_stability_ignores_animation_outside_the_page():
    cfg = StabilityConfig(interval_s=0, min_wait_s=0, required_stable_frames=3, timeout_s=2)
    n = [0]

    def grab():
        n[0] += 1
        img = Image.new("RGB", (800, 600), (29, 29, 29))
        ImageDraw.Draw(img).rectangle([200, 20, 600, 580], fill="white")          # the page
        ImageDraw.Draw(img).rectangle([0, 0, 60, 600], fill=(n[0] * 37 % 255,) * 3)  # animated side UI
        return img

    assert not wait_for_stable(grab, cfg, sleep=lambda s: None).stable
    n[0] = 0
    assert wait_for_stable(grab, cfg, sleep=lambda s: None, region=(188, 8, 612, 592)).stable


def test_default_timings_capture_pages_well_under_10_seconds(doc, tmp_path):
    """Shipped stability/retry defaults, simulated reader that renders progressively."""
    cfg = fast_config(doc)
    cfg.capture.stability = build_config({}).capture.stability       # real defaults, not test speed-ups
    assert cfg.capture.stability.timeout_s == 10.0 and build_config({}).capture.retries == 2
    sc = Scenario([make_page(i) for i in range(1, 7)], loading_grabs=2)
    h = Harness(cfg, tmp_path / "r", lambda c, e: FakeViewer(sc, cfg.viewer, e, c), lambda a: FakeSource(a))
    t0 = time.monotonic()
    assert h.run() == EXIT_OK
    per_page = (time.monotonic() - t0) / 6
    assert per_page < 2.2, per_page          # includes start-up over 6 pages; was ~2.7 s/page
    log = [json.loads(line) for line in (tmp_path / "r" / "logs" / "harness.jsonl").read_text().splitlines()]
    prog = [e for e in log if e["event"] == "progress"]
    assert len(prog) == 6 and all(e["seconds"] < 10 for e in prog)
    assert "render wait" in prog[0]["msg"] and "page number" in prog[0]["msg"]
    m = json.loads((tmp_path / "r" / "manifest.json").read_text())
    assert set(m["timings"]["capture_breakdown_s"]) >= {"render_wait", "dialog_checks", "page_number"}
    print(f"{per_page:.2f} s/page with default timings")


@pytest.mark.skipif(sys.platform != "win32", reason="win32 adapter imports Windows-only code")
def test_acrobat_taskbar_pin_is_not_a_focus_steal(monkeypatch):
    """Acrobat's own taskbar pin pop-up holding the foreground is not another program taking focus."""
    from doc_capture_harness.viewers import win32 as w32
    ad = w32.Win32ViewerAdapter.__new__(w32.Win32ViewerAdapter)
    ad.pid = 5940
    monkeypatch.setattr(w32.pw, "foreground_window", lambda: 197320)
    monkeypatch.setattr(w32.pw, "get_class_name", lambda h: "AcrobatTaskbarPinWnd")
    monkeypatch.setattr(w32.pw, "window_pid", lambda h: 5940)
    assert ad.foreground_is_transient()
    monkeypatch.setattr(w32.pw, "window_pid", lambda h: 1234)      # a different process: a real steal
    assert not ad.foreground_is_transient()
