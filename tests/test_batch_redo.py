"""Batch redo: finished documents are skipped by default and captured again with redo=True."""
import json

from doc_capture_harness.batch import Batch, _slug, discover
from doc_capture_harness.state import sha256_file

RAW = {
    "viewer": {"profile": "generic"},
    "safety": {"estop_hotkey": None},
    "logging": {"console": False},
}


class CountingFactories:
    """Records each launch, then fails the document at once (no viewer needed)."""

    def __init__(self):
        self.launched = []

    def __call__(self, cfg):
        self.launched.append(cfg.document.path)
        raise RuntimeError("stub: no viewer in tests")


def _finished_batch(tmp_path):
    """A batch whose one document is recorded as completed, with its PDF and an old run folder."""
    folder = tmp_path / "in"
    folder.mkdir()
    doc = folder / "a.pdf"
    doc.write_bytes(b"%PDF-1.7 a")
    out = tmp_path / "out"
    out.mkdir()
    pdf = out / "a.ocr.pdf"
    pdf.write_bytes(b"%PDF-1.7 result")
    items = discover([str(folder)], exclude=out)
    key = _slug(items[0].path)
    runs = out.parent / "out_details" / "_runs"
    old_run = runs / key
    old_run.mkdir(parents=True)
    (old_run / "manifest.json").write_text("{}", encoding="utf-8")
    runs.mkdir(parents=True, exist_ok=True)
    state = {"created": "x", "docs": {key: {"path": str(items[0].path), "sha256": sha256_file(doc),
                                          "status": "completed", "pdf": str(pdf), "run_dir": str(old_run)}}}
    (runs / "batch.json").write_text(json.dumps(state), encoding="utf-8")
    return items, out, old_run


def test_already_done_lists_finished_documents(tmp_path):
    items, out, _ = _finished_batch(tmp_path)
    b = Batch(RAW, items, out, CountingFactories(), echo=lambda m: None)
    assert [i.path.name for i in b.already_done()] == ["a.pdf"]


def test_already_done_ignores_changed_or_missing_pdf(tmp_path):
    items, out, _ = _finished_batch(tmp_path)
    items[0].path.write_bytes(b"%PDF-1.7 changed")          # file changed -> not done
    assert Batch(RAW, items, out, CountingFactories(), echo=lambda m: None).already_done() == []


def test_run_skips_finished_document_by_default(tmp_path):
    items, out, old_run = _finished_batch(tmp_path)
    f, msgs = CountingFactories(), []
    Batch(RAW, items, out, f, echo=msgs.append).run()
    assert f.launched == []
    assert any("already done, skipping" in m for m in msgs)
    assert old_run.exists()


def test_run_with_redo_captures_again_and_keeps_old_run(tmp_path):
    items, out, old_run = _finished_batch(tmp_path)
    f, msgs = CountingFactories(), []
    b = Batch(RAW, items, out, f, echo=msgs.append, redo=True)
    assert b.redo is True
    b.run()
    assert len(f.launched) == 1                               # the document was captured again
    assert not old_run.exists()                               # old run moved aside, not deleted
    kept = [p for p in old_run.parent.iterdir() if p.name.startswith(old_run.name + ".replaced-")]
    assert len(kept) == 1 and (kept[0] / "manifest.json").exists()
    assert any("redoing (old run kept in" in m for m in msgs)
    assert not any("already done, skipping" in m for m in msgs)


def _fresh_batch(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(b"%PDF-1.7 a")
    out = tmp_path / "out"
    out.mkdir()
    return discover([str(folder)], exclude=out), out


def test_pause_file_holds_batch_until_deleted(tmp_path):
    import threading
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(b"%PDF-1.7 a")
    (folder / "b.pdf").write_bytes(b"%PDF-1.7 b")
    out = tmp_path / "out"
    out.mkdir()
    items = discover([str(folder)], exclude=out)
    pause = out.parent / "out_details" / "_runs" / "PAUSE"

    class PauseAfterFirst(CountingFactories):
        """Creates the PAUSE file during document 1; a timer deletes it (the progress page's Play)."""
        def __call__(self, cfg):
            if not self.launched:
                pause.write_text("", encoding="utf-8")
                threading.Timer(0.5, lambda: pause.unlink(missing_ok=True)).start()
            return super().__call__(cfg)

    f, msgs = PauseAfterFirst(), []
    Batch(RAW, items, out, f, echo=msgs.append).run()
    assert len(f.launched) == 2                               # doc 2 started only after the pause ended
    assert any("Paused - press play" in m for m in msgs)
    assert not pause.exists()


def test_stale_pause_from_earlier_run_is_removed_at_start(tmp_path):
    items, out = _fresh_batch(tmp_path)
    pause = out.parent / "out_details" / "_runs" / "PAUSE"
    pause.parent.mkdir(parents=True)
    pause.write_text("", encoding="utf-8")
    f, msgs = CountingFactories(), []
    Batch(RAW, items, out, f, echo=msgs.append).run()         # would hang if the stale file held it
    assert not pause.exists()
    assert len(f.launched) == 1
    assert not any("Paused" in m for m in msgs)
