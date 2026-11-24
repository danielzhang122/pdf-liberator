"""PAUSE file support: the capture loop holds while a PAUSE file exists and carries on once it is gone."""
import json
import threading

from conftest import fast_config
from doc_capture_harness.models import EndReason
from doc_capture_harness.runner import EXIT_OK, EXIT_STOPPED, Harness
from fakes import FakeSource, FakeViewer, Scenario, make_page


def make_harness(cfg, scenario, run_dir, pause_files=()):
    def af(checkpoint, estop):
        return FakeViewer(scenario, cfg.viewer, estop, checkpoint)

    def responder(reason, msg):
        raise AssertionError(f"unexpected checkpoint {reason}: {msg}")

    return Harness(cfg, run_dir, af, lambda adapter: FakeSource(adapter), responder=responder,
                   skip_ocr=True, pause_files=pause_files)


def read_manifest(run_dir):
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_pause_file_holds_run_until_deleted(doc, tmp_path):
    run_dir = tmp_path / "r"
    cfg = fast_config(doc)
    scenario = Scenario([make_page(i) for i in range(1, 4)])
    pause_file = run_dir / "PAUSE"
    seen = {"paused_flag": None, "timer_fired": False}

    def delete_later():
        # Runs on a background thread while the harness is waiting: record what the manifest
        # says mid-pause, then delete the PAUSE file so the run continues.
        seen["paused_flag"] = read_manifest(run_dir).get("paused")
        pause_file.unlink()
        seen["timer_fired"] = True

    timer = None

    def on_page_shown(n):
        nonlocal timer
        if n == 2 and timer is None:
            pause_file.write_text("pause")
            timer = threading.Timer(0.5, delete_later)
            timer.start()

    scenario.on_page_shown = on_page_shown
    h = make_harness(cfg, scenario, run_dir)
    try:
        code = h.run()
    finally:
        if timer is not None:
            timer.join(5)

    assert code == EXIT_OK
    assert seen["timer_fired"], "the PAUSE file was never deleted by the timer"
    assert seen["paused_flag"] is True, "manifest did not show paused=True during the pause"
    assert not pause_file.exists()
    manifest = read_manifest(run_dir)
    assert manifest["paused"] is False
    assert manifest["end"]["reason"] == EndReason.INDICATOR_TOTAL.value
    assert len(manifest["pages"]) == 3
    assert all(p["status"] == "captured" for p in manifest["pages"].values())


def test_stale_own_pause_file_is_removed_at_start(doc, tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "PAUSE").write_text("left over from an earlier session")
    cfg = fast_config(doc)
    h = make_harness(cfg, Scenario([make_page(i) for i in range(1, 4)]), run_dir)
    assert h.run() == EXIT_OK
    assert not (run_dir / "PAUSE").exists()
    # "paused" is only written when a pause actually happens, so it is absent or False here.
    assert read_manifest(run_dir).get("paused") is not True


def test_emergency_stop_still_works_while_paused(doc, tmp_path):
    run_dir = tmp_path / "r"
    cfg = fast_config(doc)
    scenario = Scenario([make_page(i) for i in range(1, 4)])
    timers = []

    def on_page_shown(n):
        if n == 2 and not timers:
            (run_dir / "PAUSE").write_text("pause")          # never deleted by this test
            stop = threading.Timer(0.5, lambda: (run_dir / cfg.safety.stop_file_name).write_text("stop"))
            timers.append(stop)
            stop.start()

    scenario.on_page_shown = on_page_shown
    h = make_harness(cfg, scenario, run_dir)
    try:
        code = h.run()
    finally:
        for t in timers:
            t.join(5)
    assert code == EXIT_STOPPED
    # The stop ended the pause, and the flag is still cleared on the way out.
    assert read_manifest(run_dir)["paused"] is False


def test_paused_time_does_not_count_toward_time_limit(doc, tmp_path):
    run_dir = tmp_path / "r"
    # 0.1 min = 6 s limit. Active run time is about 2 s; the 4.5 s pause on top of that would
    # exceed 6 s if paused time were counted.
    cfg = fast_config(doc, document={"time_limit_minutes": 0.1})
    scenario = Scenario([make_page(i) for i in range(1, 4)])
    timers = []

    def on_page_shown(n):
        if n == 2 and not timers:
            (run_dir / "PAUSE").write_text("pause")
            t = threading.Timer(4.5, lambda: (run_dir / "PAUSE").unlink())
            timers.append(t)
            t.start()

    scenario.on_page_shown = on_page_shown
    h = make_harness(cfg, scenario, run_dir)
    try:
        code = h.run()
    finally:
        for t in timers:
            t.join(5)
    assert code == EXIT_OK
    manifest = read_manifest(run_dir)
    assert len(manifest["pages"]) == 3
    assert manifest["end"]["reason"] == EndReason.INDICATOR_TOTAL.value
