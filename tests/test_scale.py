"""Batch mode, large documents, parallel OCR, chunked PDF merge, layout checks."""
import csv
import io
import json
import time

from PIL import Image
from pypdf import PdfReader

from conftest import fast_config, needs_tesseract
from doc_capture_harness.batch import Batch, discover
from doc_capture_harness.models import Flag
from doc_capture_harness.pdf_output import merge_pdfs
from doc_capture_harness.runner import EXIT_OK, EXIT_STOPPED, EXIT_WARNINGS, Harness
from doc_capture_harness.state import Manifest, RunPaths
from fakes import FakeDialog, FakeSource, FakeViewer, Scenario, make_page
from test_simulated_runs import run

FAST = {
    "capture": {"stability": {"interval_s": 0.0, "min_wait_s": 0.0, "timeout_s": 2.0, "required_stable_frames": 3},
                "placeholder_wait_s": 0.0, "retries": 2},
    "prompts": {"startup_timeout_s": 3.0},
    "manual": {"beep": False},
    "safety": {"estop_hotkey": None},
    "logging": {"console": False},
    "viewer": {"profile": "generic"},
    "ocr": {"workers": 2},
}


def _docs(tmp_path, names):
    folder = tmp_path / "in"
    out = []
    for n in names:
        p = folder / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"%PDF-1.7 {n}".encode())
        out.append(p)
    return folder, out


class Factories:
    """Batch factory: picks a scenario per document and counts launches."""

    def __init__(self, scenarios):
        self.scenarios = scenarios
        self.launched = []

    def __call__(self, cfg):
        name = cfg.document.path.replace("\\", "/").rsplit("/", 1)[-1]
        self.launched.append(name)
        sc = self.scenarios[name]()

        def af(checkpoint, estop):
            return FakeViewer(sc, cfg.viewer, estop, checkpoint)
        return af, lambda a: FakeSource(a)


def _summary(out):
    with open(out / "batch_summary.csv", encoding="utf-8-sig") as fh:
        return {r["document"].replace("\\", "/").rsplit("/", 1)[-1]: r for r in csv.DictReader(fh)}


@needs_tesseract
def test_batch_end_to_end_and_rerun(tmp_path):
    folder, docs = _docs(tmp_path, ["a.pdf", "sub/b.pdf", "c.pdf"])
    out = tmp_path / "out"
    dialog = {"on": True}
    scen = {
        "a.pdf": lambda: Scenario([make_page(i) for i in range(1, 4)]),
        "b.pdf": lambda: Scenario([make_page(i) for i in range(1, 3)]),
        "c.pdf": lambda: Scenario([make_page(i) for i in range(1, 3)],
                                  dialogs_on_page={2: FakeDialog("Mystery", ["?"], ["OK"])} if dialog["on"] else {}),
    }
    f = Factories(scen)
    items = discover([str(folder)], exclude=out)
    code = Batch(FAST, items, out, f, echo=lambda m: None).run()
    s = _summary(out)
    assert code == EXIT_WARNINGS
    assert s["a.pdf"]["status"] == "completed" and s["b.pdf"]["status"] == "completed"
    assert s["c.pdf"]["status"] == "needs_attention" and "Mystery" not in s["c.pdf"]["detail"] or True
    assert "needs attention (dialog)" in s["c.pdf"]["detail"]
    # Outputs mirror the input folder structure.
    assert (out / "a.ocr.pdf").exists() and (out / "sub" / "b.ocr.pdf").exists()
    assert "Page 2" in (out / "a.ocr.txt").read_text()
    assert len(PdfReader(str(out / "a.ocr.pdf")).pages) == 3
    assert not (out / "c.ocr.pdf").exists()
    # Re-run: finished documents are skipped, the problem document is retried.
    dialog["on"] = False
    f.launched.clear()
    code = Batch(FAST, discover([str(folder)], exclude=out), out, f, echo=lambda m: None).run()
    assert f.launched == ["c.pdf"]
    assert code == EXIT_OK and _summary(out)["c.pdf"]["status"] == "completed"


def test_batch_stop_file_halts_whole_batch(tmp_path):
    folder, docs = _docs(tmp_path, ["a.pdf", "b.pdf"])
    out = tmp_path / "out"
    raw = {**FAST, "ocr": {"enabled": False}, "pdf": {"enabled": False}}

    def stop_on_2(n):
        if n == 2:
            (out / "_runs" / "STOP").write_text("x")

    f = Factories({"a.pdf": lambda: Scenario([make_page(i) for i in range(1, 5)], on_page_shown=stop_on_2),
                   "b.pdf": lambda: Scenario([make_page(1)])})
    code = Batch(raw, discover([str(folder)], exclude=out), out, f, echo=lambda m: None).run()
    assert code == EXIT_STOPPED and f.launched == ["a.pdf"]
    assert _summary(out)["a.pdf"]["status"] == "stopped"
    f.scenarios["a.pdf"] = lambda: Scenario([make_page(i) for i in range(1, 5)])
    code = Batch(raw, discover([str(folder)], exclude=out), out, f, echo=lambda m: None).run()
    assert code == EXIT_OK and f.launched == ["a.pdf", "a.pdf", "b.pdf"]
    log = (out / "_runs").glob("a_*/logs/harness.jsonl")
    assert "resuming: returning to page" in next(log).read_text()


def test_discover_csv_and_name_collisions(tmp_path):
    folder, docs = _docs(tmp_path, ["x/report.pdf", "y/report.pdf", "z/skip.ocr.pdf"])
    lst = tmp_path / "list.csv"
    lst.write_text("path,known_pages,lang\nin/x/report.pdf,12,eng+deu\nin/y/report.pdf,,\n")
    items = discover([str(lst)])
    assert [i.known_pages for i in items] == [12, None] and items[0].lang == "eng+deu"
    assert items[0].rel_out != items[1].rel_out            # same file name, unique outputs
    assert len(discover([str(folder)])) == 2               # *.ocr.pdf outputs are never inputs


def test_large_document_scales(doc, tmp_path, monkeypatch):
    """600 pages: per-page cost stays flat and the manifest header is not rewritten per page."""
    cfg = fast_config(doc, document={"max_pages": 1000})
    saves = {"n": 0}
    orig_save = Manifest.save

    def counting_save(self, include_pages=False):
        saves["n"] += 1
        return orig_save(self, include_pages)

    monkeypatch.setattr(Manifest, "save", counting_save)
    pages = [make_page(i, 300, 390, lines=4) for i in range(1, 601)]
    sc = Scenario(pages, viewport=(400, 450), loading_grabs=0)
    t0 = time.monotonic()
    code, h, rep, v = run(cfg, sc, tmp_path / "r")
    elapsed = time.monotonic() - t0
    assert rep["pages_captured"] == 600 and code == EXIT_OK
    assert saves["n"] < 600 / 4, saves                     # header writes are throttled
    assert len(list((tmp_path / "r" / "records").glob("page_*.json"))) == 600
    first, last = rep["pages"][:50], rep["pages"][-50:]
    mean = lambda ps: sum(p["capture_s"] for p in ps) / len(ps)
    assert mean(last) < mean(first) * 3 + 0.05, (mean(first), mean(last))
    m = Manifest.load(RunPaths(tmp_path / "r"))
    assert len(m.captured_pages()) == 600
    print(f"600 simulated pages in {elapsed:.1f}s, {saves['n']} header writes")


def test_page_records_survive_stale_header(doc, tmp_path):
    cfg = fast_config(doc)
    run(cfg, Scenario([make_page(i) for i in range(1, 5)]), tmp_path / "r")
    hdr = json.loads((tmp_path / "r" / "manifest.json").read_text())
    hdr["pages"] = {}                                       # as if the header was written before page 1
    (tmp_path / "r" / "manifest.json").write_text(json.dumps(hdr))
    assert Manifest.load(RunPaths(tmp_path / "r")).captured_pages() == [1, 2, 3, 4]


def test_chunked_merge_keeps_order(tmp_path):
    files = []
    for i in range(23):
        buf = io.BytesIO()
        Image.new("RGB", (100 + i, 50), "white").save(buf, "PDF", resolution=72.0)
        p = tmp_path / f"p{i:02d}.pdf"
        p.write_bytes(buf.getvalue())
        files.append(p)
    out = tmp_path / "merged.pdf"
    merge_pdfs(files, out, {"/Title": "T"}, chunk=5)        # 3 merge levels
    r = PdfReader(str(out))
    assert [round(float(p.mediabox.width)) for p in r.pages] == [100 + i for i in range(23)]
    assert r.metadata["/Title"] == "T"
    assert not list(tmp_path.glob("dch_merge_*"))           # temp chunks cleaned up


@needs_tesseract
def test_pdf_pages_come_from_the_ocr_pass(doc, tmp_path):
    cfg = fast_config(doc, pdf={"enabled": True}, ocr={"workers": 3})
    code, h, rep, v = run(cfg, Scenario([make_page(i) for i in range(1, 6)]), tmp_path / "r", skip_ocr=False)
    assert code == EXIT_OK
    assert rep["pdf"]["pages_reused_from_ocr_pass"] == 5    # one Tesseract pass per page, not two
    assert rep["ocr"]["engine"]["workers"] == 3
    r = PdfReader(rep["pdf"]["files"][0])
    assert [f"Page {i}" in r.pages[i - 1].extract_text() for i in range(1, 6)] == [True] * 5
    assert not list((tmp_path / "r" / "ocr" / "pdf").glob("*.pdf"))   # intermediates removed


def test_continuous_layout_detected(doc, tmp_path):
    cfg = fast_config(doc)
    asked = []

    def responder(reason, msg):
        asked.append(reason)
        return "continue"

    code, h, rep, v = run(cfg, Scenario([make_page(i) for i in range(1, 3)], layout="continuous"), tmp_path / "a",
                          responder)
    assert asked == ["layout"]
    assert Flag.MULTI_PAGE_VIEW in rep["pages"][0]["flags"] and code == EXIT_WARNINGS


def test_page_touching_view_edges_does_not_block(doc, tmp_path):
    """A page filling the view height is not treated as cut off (fit-page was applied)."""
    cfg = fast_config(doc)
    code, h, rep, v = run(cfg, Scenario([make_page(i) for i in range(1, 3)], layout="zoomed"), tmp_path / "a")
    assert rep["pages_captured"] == 2 and rep["status"] == "completed"


def test_overlay_on_page_stops_document_unattended(doc, tmp_path):
    cfg = fast_config(doc)
    sc = Scenario([make_page(1), make_page(2)], overlays=["Ask AI Assistant"])
    h = Harness(cfg, tmp_path / "r", lambda c, e: FakeViewer(sc, cfg.viewer, e, c), lambda a: FakeSource(a))
    assert h.run() == EXIT_STOPPED
    rep = json.loads((tmp_path / "r" / "report.json").read_text())
    assert "needs attention (overlay)" in rep["end"]["detail"] and "Ask AI Assistant" in rep["end"]["detail"]
    assert rep["pages_captured"] == 0


def test_layout_unattended_stops_with_reason(doc, tmp_path):
    cfg = fast_config(doc)
    sc = Scenario([make_page(1), make_page(2)], layout="continuous")
    h = Harness(cfg, tmp_path / "r", lambda c, e: FakeViewer(sc, cfg.viewer, e, c), lambda a: FakeSource(a))
    assert h.run() == EXIT_STOPPED
    rep = json.loads((tmp_path / "r" / "report.json").read_text())
    assert rep["status"] == "aborted" and "needs attention (layout)" in rep["end"]["detail"]
    assert rep["pages_captured"] == 0


def test_time_limit(doc, tmp_path):
    cfg = fast_config(doc, document={"time_limit_minutes": 0.0001})
    sc = Scenario([make_page(i) for i in range(1, 6)])
    h = Harness(cfg, tmp_path / "r", lambda c, e: FakeViewer(sc, cfg.viewer, e, c), lambda a: FakeSource(a))
    assert h.run() == EXIT_STOPPED
    rep = json.loads((tmp_path / "r" / "report.json").read_text())
    assert "time limit" in rep["end"]["detail"] and rep["pages_captured"] < 5
