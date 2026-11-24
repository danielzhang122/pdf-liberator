"""Local progress page: `python -m doc_capture_harness dashboard <folder>`.

Serves http://127.0.0.1:<port>/ which polls /status.json. Every folder under
<folder> holding a manifest.json is one run (works for single runs and batches).
Read-only; it never touches the run while the capture writes to it.
"""
from __future__ import annotations

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .state import Manifest, RunPaths


def run_status(root: Path) -> dict:
    paths = RunPaths(root)
    try:
        d = Manifest.load(paths).data
    except (OSError, ValueError) as exc:
        return {"dir": str(root), "error": str(exc)}
    pages = d["pages"].values()
    captured = sum(1 for p in pages if p.get("status") == "captured")
    return {
        "dir": str(root),
        "document": Path((d.get("document") or {}).get("path") or root.name).name,
        "status": d.get("status"),
        "updated": d.get("updated"),
        "expected": (d.get("expected_pages") or {}).get("count"),
        "captured": captured,
        "ocr_done": sum(1 for p in pages if p.get("ocr")),
        "problems": sum(1 for p in pages if p.get("status") not in (None, "captured")),
        "errors": len((d.get("events") or {}).get("errors") or []),
        "pdf": (d.get("pdf") or {}).get("path"),
    }


def collect(folder: Path) -> list[dict]:
    runs = [folder] if (folder / "manifest.json").exists() else \
        sorted({m.parent for m in folder.rglob("manifest.json")})
    return [run_status(r) for r in runs]


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Capture progress</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fff;--fg:#111;--mut:#666;--bar:#2563eb;--line:#ddd;--bad:#c2410c}
@media(prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#999;--bar:#60a5fa;--line:#333;--bad:#fb923c}}
body{background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif;margin:0 auto;max-width:900px;padding:16px}
.run{border:1px solid var(--line);border-radius:8px;padding:12px;margin:10px 0}
.top{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.mut{color:var(--mut);font-size:13px}.bad{color:var(--bad)}
progress{width:100%;height:14px;accent-color:var(--bar)}
</style></head><body><h1>Capture progress</h1><p class="mut" id="t">loading…</p><div id="runs"></div>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function tick(){
  try{
    const runs=await (await fetch("status.json",{cache:"no-store"})).json();
    document.getElementById("runs").innerHTML=runs.length?runs.map(r=>{
      if(r.error)return `<div class="run bad">${esc(r.dir)}: ${esc(r.error)}</div>`;
      const tot=r.expected||r.captured||1;
      return `<div class="run"><div class="top"><b>${esc(r.document)}</b><span>${esc(r.status)}</span></div>
      <label>Captured ${r.captured}${r.expected?" / "+r.expected:""}<progress value="${r.captured}" max="${tot}"></progress></label>
      <label>OCR ${r.ocr_done} / ${r.captured}<progress value="${r.ocr_done}" max="${r.captured||1}"></progress></label>
      <div class="mut">${r.problems?`<span class="bad">${r.problems} page(s) need review</span> · `:""}${r.errors?`<span class="bad">${r.errors} error(s)</span> · `:""}updated ${esc(r.updated)}${r.pdf?" · PDF: "+esc(r.pdf):""}<br>${esc(r.dir)}</div></div>`;
    }).join(""):"<p>No runs found yet.</p>";
    document.getElementById("t").textContent="Refreshed "+new Date().toLocaleTimeString();
  }catch(e){document.getElementById("t").textContent="Dashboard server not reachable";}
}
tick();setInterval(tick,2000);
</script></body></html>"""


def serve(folder: Path, port: int = 8765, open_browser: bool = True) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/status.json":
                body, ctype = json.dumps(collect(folder)).encode(), "application/json"
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # localhost only: shows file paths
    url = f"http://127.0.0.1:{port}/"
    print(f"Progress page: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
