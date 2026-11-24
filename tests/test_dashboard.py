import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from doc_capture_harness import dashboard
from doc_capture_harness.state import Manifest, RunPaths


def _run(tmp_path):
    paths = RunPaths(tmp_path / "out" / "doc_1")
    paths.ensure()
    m = Manifest.create(paths, document={"path": "C:/x/book.pdf"}, viewer="generic",
                        config_fingerprint="f", run_id="r")
    m.data["expected_pages"] = {"count": 4, "source": "test"}
    m.save()
    m.set_page(1, {"status": "captured", "ocr": {"words": 5}})
    m.set_page(2, {"status": "captured"})
    m.set_page(3, {"status": "blank_suspect"})
    return tmp_path / "out"


def test_collect_counts_progress(tmp_path):
    [r] = dashboard.collect(_run(tmp_path))
    assert (r["document"], r["expected"], r["captured"], r["ocr_done"], r["problems"]) == ("book.pdf", 4, 2, 1, 1)


def test_broken_manifest_reported_not_raised(tmp_path):
    d = tmp_path / "bad"; d.mkdir(); (d / "manifest.json").write_text("{")
    assert "error" in dashboard.collect(tmp_path)[0]


def test_server_serves_page_and_json(tmp_path, monkeypatch):
    folder = _run(tmp_path)
    started = {}
    real = ThreadingHTTPServer

    class Srv(real):
        def __init__(self, addr, h):
            super().__init__(("127.0.0.1", 0), h); started["s"] = self
    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", Srv)
    t = threading.Thread(target=dashboard.serve, args=(folder, 0, False), daemon=True); t.start()
    while "s" not in started: pass
    base = f"http://127.0.0.1:{started['s'].server_address[1]}/"
    assert b"Capture progress" in urllib.request.urlopen(base).read()
    assert json.loads(urllib.request.urlopen(base + "status.json").read())[0]["captured"] == 2
    started["s"].shutdown()
