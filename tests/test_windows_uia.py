"""Real-Windows check of the native UI Automation code in viewers/win32.py.

Runs only on Windows (the windows-setup CI job). Notepad stands in for the
reader: it opens a file whose text is "Page 3 of 12", which the page-number
search must find through the native walker, read through the Value pattern,
and then serve from its cache without searching again.
"""
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation only")


@pytest.fixture
def notepad(tmp_path):
    from doc_capture_harness import platform_win as pw
    from doc_capture_harness.config import build_config
    from doc_capture_harness.estop import EmergencyStopMonitor
    from doc_capture_harness.inputs import InputDriver
    from doc_capture_harness.viewers.win32 import Win32ViewerAdapter

    pw.enable_dpi_awareness()
    doc = tmp_path / "uiaprobe.txt"
    doc.write_text("Page 3 of 12", encoding="utf-8")
    cfg = build_config({"viewer": {
        "profile": "generic", "executable": r"C:\Windows\System32\notepad.exe",
        "launch_args": ["{document}"], "process_names": ["notepad.exe"],
        "window_title_re": "{stem}", "launch_timeout_s": 20}})

    def checkpoint(reason, msg):
        raise AssertionError(f"unexpected checkpoint {reason}: {msg}")

    estop = EmergencyStopMonitor(None, None)
    ad = Win32ViewerAdapter(cfg.viewer, estop, checkpoint, InputDriver(estop))
    ad.launch(doc)
    ad.attach(doc, 20)
    yield ad
    subprocess.run(["taskkill", "/PID", str(ad.pid), "/F"], capture_output=True)


def _edit_hwnd(pw, hwnd):
    for h, cls, _t, r in pw.child_windows(hwnd):
        if "edit" in cls.lower() and r.area > 0:
            return h
    return None


def test_native_walker_and_pane_skip(notepad):
    from doc_capture_harness import platform_win as pw

    els = notepad._walk([notepad.hwnd], lambda e: True, max_nodes=500, max_s=5)
    assert els, "native walker returned nothing"
    edit = _edit_hwnd(pw, notepad.hwnd)
    if edit:
        notepad._pane_hwnd = edit
        skipped = notepad._walk([notepad.hwnd], lambda e: True, max_nodes=500, max_s=5)
        assert all(e.CurrentNativeWindowHandle != edit for e in skipped)
        notepad._pane_hwnd = None


def test_page_number_read_natively_and_cached(notepad):
    notepad._indicator_cache.reset()
    t0 = time.monotonic()
    ind = notepad.read_page_indicator()
    first = time.monotonic() - t0
    assert (ind.current, ind.total) == (3, 12), ind
    t0 = time.monotonic()
    for _ in range(15):
        assert notepad.read_page_indicator().current == 3
    later = (time.monotonic() - t0) / 15
    assert notepad._indicator_cache.scans == 1          # searched once, then served from cache
    assert later < 0.5, later
    print(f"first read {first:.2f}s, cached read {later * 1000:.0f} ms")


def test_window_description_is_cached(notepad, monkeypatch):
    from doc_capture_harness import platform_win as pw

    w = next(x for x in pw.top_level_windows() if x.hwnd == notepad.hwnd)
    d1 = notepad._describe(w, True)
    monkeypatch.setattr(notepad, "_walk", lambda *a, **k: (_ for _ in ()).throw(AssertionError("walked again")))
    monkeypatch.setattr(pw, "child_windows", lambda *a, **k: (_ for _ in ()).throw(AssertionError("enumerated")))
    d2 = notepad._describe(w, True)
    assert (d2.texts, d2.buttons) == (d1.texts, d1.buttons)


def test_set_window_text_into_another_process(notepad):
    """WM_SETTEXT prototype used to fill Acrobat's go-to-page box without key presses."""
    from doc_capture_harness import platform_win as pw

    edit = _edit_hwnd(pw, notepad.hwnd)
    if edit is None:
        pytest.skip("this Notepad has no Win32 edit control")
    assert pw.set_window_text(edit, "Page 7 of 12")
    notepad._indicator_cache.reset()
    assert notepad.read_page_indicator().current == 7


def test_background_posted_keys_without_focus(notepad):
    """Background mode: keys posted to the window reach it while another window has
    the focus, the focus never moves, and PrintWindow still captures it."""
    from doc_capture_harness import platform_win as pw

    edit = _edit_hwnd(pw, notepad.hwnd)
    if edit is None:
        pytest.skip("this Notepad has no Win32 edit control")
    notepad.cfg.background = True
    notepad._pane_hwnd = edit
    notepad.cfg.document_pane.class_name = pw.get_class_name(edit)
    fg = pw.foreground_window()
    notepad.send_keys("{END}{BACKSPACE}{BACKSPACE}20")
    time.sleep(0.5)
    assert pw.foreground_window() == fg
    assert notepad.is_foreground()
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.SendMessageW(edit, 0x000D, 256, buf)   # WM_GETTEXT
    assert buf.value.endswith("of 20"), buf.value               # the posted keys edited the text
    assert notepad.grab_window() is not None
