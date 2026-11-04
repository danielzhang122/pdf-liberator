"""End-to-end runs of the real runner against the simulated reader."""
import json
import os
import stat

from conftest import fast_config, needs_tesseract
from doc_capture_harness.models import EndReason, Flag
from doc_capture_harness.runner import EXIT_FAILED, EXIT_OK, EXIT_STOPPED, EXIT_WARNINGS, Harness
from fakes import FakeDialog, FakeSource, FakeViewer, Scenario, make_page


def run(cfg, scenario, run_dir, responder=None, resume=False, skip_ocr=True):
    holder = {}

    def af(checkpoint, estop):
        holder["v"] = FakeViewer(scenario, cfg.viewer, estop, checkpoint)
        return holder["v"]

    def sf(adapter):
        return FakeSource(adapter)

    def default_responder(reason, msg):
        raise AssertionError(f"unexpected checkpoint {reason}: {msg}")

    h = Harness(cfg, run_dir, af, sf, responder=responder or default_responder, resume=resume,
                skip_ocr=skip_ocr)
    code = h.run()
    report = json.loads((run_dir / "report.json").read_text())
    return code, h, report, holder.get("v")


def flags(report, page):
    return report["pages"][page - 1]["flags"]


def test_indicator_driven_run(doc, tmp_path):
    cfg = fast_config(doc)
    sc = Scenario([make_page(i) for i in range(1, 6)])
    code, h, rep, v = run(cfg, sc, tmp_path / "r1")
    assert code == EXIT_OK, rep
    assert rep["pages_captured"] == 5
    assert rep["end"]["reason"] == EndReason.INDICATOR_TOTAL.value
    assert not rep["end_is_uncertain"]
    assert sorted(os.listdir(tmp_path / "r1" / "pages")) == [f"page_000{i}.png" for i in range(1, 6)]
    # Cropped to the page, not the grey viewport.
    w, hgt = rep["pages"][0]["size"]
    assert w < 800 and hgt < 900
    # Original untouched; opened copy is read-only.
    assert rep["document_unchanged"]
    copy = tmp_path / "r1" / "work" / doc.name
    assert v.launched == copy and not (os.stat(copy).st_mode & stat.S_IWRITE)


def test_identical_pages_with_indicator_are_kept(doc, tmp_path):
    cfg = fast_config(doc)
    p2 = make_page(2)
    sc = Scenario([make_page(1), p2, p2.copy(), make_page(4)])
    code, h, rep, v = run(cfg, sc, tmp_path / "r")
    assert rep["pages_captured"] == 4
    assert Flag.IDENTICAL_PREV in flags(rep, 3)


def test_no_indicator_end_confirmed_by_probe(doc, tmp_path):
    cfg = fast_config(doc)
    sc = Scenario([make_page(i) for i in range(1, 5)], indicator=False)
    code, h, rep, v = run(cfg, sc, tmp_path / "r")
    assert rep["pages_captured"] == 4
    assert rep["end"]["reason"] == EndReason.STALL_CONFIRMED.value
    assert rep["end_is_uncertain"] and code == EXIT_WARNINGS
    assert Flag.NO_INDICATOR in flags(rep, 2)


def test_no_indicator_identical_pages_are_flagged_and_checkpointed(doc, tmp_path):
    """Identical consecutive pages without an indicator are ambiguous: a retry
    key press may skip a page. The harness must flag it for review, and with a
    known page count it asks the operator instead of declaring the end."""
    cfg = fast_config(doc, document={"known_page_count": 4})
    p2 = make_page(2)
    sc = Scenario([make_page(1), p2, p2.copy(), make_page(4)], indicator=False)
    asked = []

    def responder(reason, msg):
        asked.append(reason)
        return "continue"       # operator confirms the reader now shows page 3

    code, h, rep, v = run(cfg, sc, tmp_path / "r", responder)
    assert asked == ["navigation_failed"]
    assert rep["pages_captured"] == 4
    assert rep["end"]["reason"] == EndReason.KNOWN_PAGE_COUNT.value
    assert Flag.NAV_RETRY_UNVERIFIED in flags(rep, 3)
    assert Flag.MANUAL_OVERRIDE in flags(rep, 4)
    assert code == EXIT_WARNINGS
    assert {r["page"] for r in rep["pages_needing_review"]} == {3, 4}


def test_flaky_navigation_is_retried(doc, tmp_path):
    cfg = fast_config(doc)
    sc = Scenario([make_page(i) for i in range(1, 4)], stuck_next={1: 1})
    code, h, rep, v = run(cfg, sc, tmp_path / "r")
    assert code == EXIT_OK and rep["pages_captured"] == 3


def test_max_pages_limit(doc, tmp_path):
    cfg = fast_config(doc, document={"max_pages": 3})
    sc = Scenario([make_page(i) for i in range(1, 7)], indicator=False)
    code, h, rep, v = run(cfg, sc, tmp_path / "r")
    assert rep["pages_captured"] == 3
    assert rep["end"]["reason"] == EndReason.MAX_PAGES.value and rep["end_is_uncertain"]


def test_dialogs_known_and_unknown(doc, tmp_path):
    cfg = fast_config(doc, prompts={"rules": [
        {"name": "tip", "title_re": "Tip of the day", "action": "click", "button": "Close"}]})
    sc = Scenario([make_page(i) for i in range(1, 5)], dialogs_on_page={
        2: FakeDialog("Tip of the day", ["Did you know?"], ["Close"]),
        3: FakeDialog("Mystery", ["Unexpected thing"], ["OK"]),
        4: FakeDialog("Security Warning", ["Enable JavaScript for this document?"], ["Enable", "Close"]),
    })
    holder = {}
    asked = []

    def responder(reason, msg):
        asked.append((reason, msg.split("\n")[0]))
        holder["v"].dismiss_all()   # operator handles it in the reader
        return "retry"

    def af(checkpoint, estop):
        holder["v"] = FakeViewer(sc, cfg.viewer, estop, checkpoint)
        return holder["v"]

    h = Harness(cfg, tmp_path / "r", af, lambda a: FakeSource(a), responder=responder, skip_ocr=True)
    h.run()
    rep = json.loads((tmp_path / "r" / "report.json").read_text())
    assert rep["pages_captured"] == 4
    assert [a[0] for a in asked] == ["dialog", "dialog"]       # unknown + security, not the tip
    assert rep["prompts_handled"] >= 3 and rep["manual_checkpoints"] == 2


def test_emergency_stop_then_resume(doc, tmp_path):
    cfg = fast_config(doc)
    rd = tmp_path / "r"
    pages = [make_page(i) for i in range(1, 7)]

    def stop_on_4(n):
        if n == 4:
            (rd / "STOP").write_text("stop")

    code, h, rep, v = run(cfg, Scenario(pages, on_page_shown=stop_on_4), rd)
    assert code == EXIT_STOPPED and rep["status"] == "stopped"
    done = rep["pages_captured"]
    assert 3 <= done < 6

    # Resume: the stale STOP file is cleared and capture continues where it stopped.
    sc2 = Scenario(pages)
    code, h, rep, v = run(cfg, sc2, rd, resume=True)
    assert code == EXIT_OK, rep
    assert rep["pages_captured"] == 6 and rep["sessions"] == 2
    log = (rd / "logs" / "harness.jsonl").read_text()
    assert f"returning to page {done} (last captured)" in log


def test_resume_refuses_changed_document(doc, tmp_path):
    cfg = fast_config(doc)
    rd = tmp_path / "r"
    run(cfg, Scenario([make_page(1), make_page(2)]), rd)
    before = (rd / "manifest.json").read_text()
    doc.write_bytes(b"changed")
    code, h, rep, v = run(cfg, Scenario([make_page(1)]), rd, resume=True)
    assert code == EXIT_FAILED
    assert (rd / "manifest.json").read_text() == before        # previous run untouched
    assert "hash changed" in (rd / "logs" / "harness.jsonl").read_text()


def test_scroll_mode_stitches_tall_pages(doc, tmp_path):
    cfg = fast_config(doc, capture={"mode": "scroll", "scroll": {"wheel_notches": 6}})
    pages = [make_page(i, 600, 1900, lines=36) for i in range(1, 3)]
    sc = Scenario(pages, fit="width", viewport=(640, 700), loading_grabs=1)
    code, h, rep, v = run(cfg, sc, tmp_path / "r")
    assert rep["pages_captured"] == 2, rep
    w, hgt = rep["pages"][0]["size"]
    scaled_h = int(1900 * (600 / 600))
    assert abs(hgt - scaled_h) < 30, (w, hgt)
    assert Flag.STITCH_UNCERTAIN not in flags(rep, 1)


def test_unattended_abort_on_unknown_dialog(doc, tmp_path):
    cfg = fast_config(doc)
    sc = Scenario([make_page(1), make_page(2)], dialogs_on_page={2: FakeDialog("Mystery", ["?"], ["OK"])})

    def af(checkpoint, estop):
        return FakeViewer(sc, cfg.viewer, estop, checkpoint)

    h = Harness(cfg, tmp_path / "r", af, lambda a: FakeSource(a), skip_ocr=True)   # manual.mode=abort
    code = h.run()
    rep = json.loads((tmp_path / "r" / "report.json").read_text())
    assert code == EXIT_STOPPED and rep["status"] == "aborted" and rep["pages_captured"] == 1
    assert any(p.endswith(".png") for p in os.listdir(tmp_path / "r" / "diagnostics"))


@needs_tesseract
def test_full_run_with_ocr(doc, tmp_path):
    cfg = fast_config(doc)
    sc = Scenario([make_page(i) for i in range(1, 4)])
    code, h, rep, v = run(cfg, sc, tmp_path / "r", skip_ocr=False)
    assert code == EXIT_OK, rep["pages_needing_review"]
    ocr_dir = tmp_path / "r" / "ocr"
    for i in range(1, 4):
        assert f"Page {i}" in (ocr_dir / f"page_000{i}.txt").read_text()
    combined = (ocr_dir / "combined.txt").read_text()
    assert combined.index("===== Page 1 =====") < combined.index("===== Page 2 =====") < combined.index("===== Page 3 =====")
    assert rep["ocr"]["mean_of_page_mean_conf"] > 80
    md = (tmp_path / "r" / "report.md").read_text()
    assert "Per-page detail" in md


def test_last_page_known_from_goto_dialog_ends_and_merges(doc, tmp_path):
    """Field case: 11 pages, no readable page number, Acrobat ignores 'previous page'.
    The page count comes from the reader's go-to-page dialog, so capture stops exactly
    after page 11 (no end probe needed) and the merged PDF is built."""
    from pypdf import PdfReader

    cfg = fast_config(doc, pdf={"enabled": True, "text_layer": False})
    sc = Scenario([make_page(i) for i in range(1, 12)], indicator=False, prev_broken=True, goto_dialog=True)
    code, h, rep, v = run(cfg, sc, tmp_path / "r")
    assert code == EXIT_OK and rep["status"] == "completed" and rep["pages_captured"] == 11
    assert rep["end"]["reason"] == EndReason.KNOWN_PAGE_COUNT.value and not rep["end_is_uncertain"]
    assert rep["end"]["detail"] == "viewer_page_count=11"
    assert len(PdfReader(rep["pdf"]["files"][0]).pages) == 11


def test_unresponsive_navigation_without_page_count_is_never_a_silent_end(doc, tmp_path):
    """Without any page count, 'keys stopped working' must not be mistaken for the end
    (the real-Acrobat benchmark saw this mid-document): the document is set aside."""
    cfg = fast_config(doc)
    sc = Scenario([make_page(i) for i in range(1, 12)], indicator=False, prev_broken=True,
                  stuck_next={5: 99})
    h = Harness(cfg, tmp_path / "r", lambda c, e: FakeViewer(sc, cfg.viewer, e, c), lambda a: FakeSource(a),
                skip_ocr=True)
    assert h.run() == EXIT_STOPPED
    rep = json.loads((tmp_path / "r" / "report.json").read_text())
    assert rep["status"] == "aborted" and rep["pages_captured"] == 5
    assert "navigation" in rep["end"]["detail"]


def test_resume_of_run_stopped_at_last_page_finishes_and_merges(doc, tmp_path):
    """Exactly the reported state: all 11 pages captured, then the old version stopped with
    'navigation_failed'. Resuming must go back to page 11, see it is the end, and build the PDF
    without asking anything (unattended)."""
    from pypdf import PdfReader
    from doc_capture_harness.state import Manifest, RunPaths

    pages = [make_page(i) for i in range(1, 12)]
    rd = tmp_path / "r"
    cfg = fast_config(doc, pdf={"enabled": True, "text_layer": False}, viewer={"goto_method": "dialog"})
    run(cfg, Scenario(pages, indicator=False), rd)
    m = Manifest.load(RunPaths(rd))
    m.data.update(status="aborted", status_capture=None,
                  end={"reason": "user_abort", "detail": "needs attention (navigation_failed): ...", "last_page": 11})
    m.data.pop("pdf", None)
    m.save(include_pages=True)
    for f in (rd / "output").glob("*.pdf"):
        f.unlink()

    sc = Scenario(pages, indicator=False, prev_broken=True, goto_dialog=True)
    code, h, rep, v = run(cfg, sc, rd, resume=True)          # any checkpoint fails the test
    assert rep["end"]["reason"] == EndReason.KNOWN_PAGE_COUNT.value
    assert rep["status"] == "completed" and rep["pages_captured"] == 11
    assert v.page == 10                                       # went straight to page 11 (0-based 10)
    assert len(PdfReader(rep["pdf"]["files"][0]).pages) == 11
    log = (rd / "logs" / "harness.jsonl").read_text()
    assert "returning to page 11" in log


def test_popup_stealing_a_page_turn_is_not_mistaken_for_the_next_page(doc, tmp_path):
    """Real-Acrobat benchmark: a 'pin to taskbar' prompt took the key press and its
    appearance was counted as a page turn, losing a page (19 of 20). The pop-up must be
    waited out and the key retried, so every page is captured."""
    cfg = fast_config(doc, prompts={"obstruction_wait_s": 10})
    pop = FakeDialog("Adobe Acrobat", ["Pin Adobe Acrobat to your taskbar?"], ["Pin", "Not now"],
                     owned=False, ttl=3, draw=True)
    sc = Scenario([make_page(i) for i in range(1, 7)], indicator=False, popup_on_next={2: pop})
    code, h, rep, v = run(cfg, sc, tmp_path / "r")
    assert rep["pages_captured"] == 6, rep["end"]
    assert rep["end"]["reason"] == EndReason.STALL_CONFIRMED.value
    assert not any(Flag.DUPLICATE in p["flags"] for p in rep["pages"])


def _saved_pages_match(rd, pages):
    """Each saved page image is the expected document page (by image hash)."""
    from PIL import Image
    from doc_capture_harness.imaging import dhash, hamming
    from doc_capture_harness.state import Manifest, RunPaths
    m = Manifest.load(RunPaths(rd))
    out = []
    for n in m.captured_pages():
        img = Image.open(rd / m.page(n)["image"])
        best = min(range(len(pages)), key=lambda i: hamming(dhash(img), dhash(pages[i].resize(img.size))))
        out.append(best + 1)
    return out


def test_delayed_key_moving_two_pages_loses_a_page_with_keys(doc, tmp_path):
    """Real-Acrobat benchmark: while another window held the focus, a delayed key press was
    applied late and Acrobat moved two pages. With key navigation that silently skips a page."""
    pages = [make_page(i) for i in range(1, 9)]
    cfg = fast_config(doc, viewer={"navigate_by": "keys"})
    run(cfg, Scenario(pages, indicator=False, goto_dialog=True, double_next_on={3}), tmp_path / "r",
        responder=lambda r, m: "skip")
    assert 4 not in _saved_pages_match(tmp_path / "r", pages)          # page 4 was skipped


def test_goto_navigation_captures_every_page_exactly_once(doc, tmp_path):
    """Navigating by page number through the go-to-page dialog is immune to lost, delayed or
    doubled key presses and to pop-ups: every page is captured once, in order."""
    pages = [make_page(i) for i in range(1, 9)]
    cfg = fast_config(doc, viewer={"navigate_by": "goto"}, prompts={"obstruction_wait_s": 10})
    pop = FakeDialog("Adobe Acrobat", ["Pin Adobe Acrobat to your taskbar?"], ["Pin", "Not now"],
                     owned=False, ttl=3, draw=True)
    sc = Scenario(pages, indicator=False, goto_dialog=True, prev_broken=True, double_next_on={3},
                  stuck_next={5: 99}, popup_on_next={2: pop})
    code, h, rep, v = run(cfg, sc, tmp_path / "r")
    assert code == EXIT_OK and rep["pages_captured"] == 8
    assert rep["end"]["reason"] == EndReason.KNOWN_PAGE_COUNT.value
    assert _saved_pages_match(tmp_path / "r", pages) == list(range(1, 9))
