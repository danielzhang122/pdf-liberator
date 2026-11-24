import json
import threading
import urllib.error
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
    real = dashboard._Server

    class Srv(real):
        def __init__(self, addr, h):
            super().__init__(("127.0.0.1", 0), h); started["s"] = self
    monkeypatch.setattr(dashboard, "_Server", Srv)
    t = threading.Thread(target=dashboard.serve, args=(folder, 0, False), daemon=True); t.start()
    while "s" not in started: pass
    base = f"http://127.0.0.1:{started['s'].server_address[1]}/"
    assert b"Capture progress" in urllib.request.urlopen(base).read()
    assert json.loads(urllib.request.urlopen(base + "status.json").read())["runs"][0]["captured"] == 2
    started["s"].shutdown()


def _post(url, headers):
    req = urllib.request.Request(url, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_pause_play_stop_files_and_header_check(tmp_path):
    folder = _run(tmp_path)
    url = dashboard.start_in_background(folder, port=0, open_browser=False)
    ok = {"X-Capture-Control": "1"}
    assert _post(url + "pause", {})[0] == 403  # no header: refused
    assert not (folder / "PAUSE").exists()
    code, st = _post(url + "pause", ok)
    assert code == 200 and (folder / "PAUSE").exists() and st["paused"] is True
    code, st = _post(url + "play", ok)
    assert code == 200 and not (folder / "PAUSE").exists() and st["paused"] is False
    code, st = _post(url + "stop", ok)
    assert code == 200 and (folder / "STOP").exists() and st["stopping"] is True
    assert _post(url + "nope", ok)[0] == 404
    assert "paused" in json.loads(urllib.request.urlopen(url + "status.json").read())["runs"][0]


def test_start_in_background_falls_back_when_port_busy(tmp_path):
    blocker = dashboard.make_server(tmp_path, 0)  # holds a port the same way serve() does
    port = blocker.server_address[1]
    try:
        url = dashboard.start_in_background(_run(tmp_path), port=port, open_browser=False)
        assert url != f"http://127.0.0.1:{port}/"
        assert b"Capture progress" in urllib.request.urlopen(url).read()
    finally:
        blocker.server_close()
