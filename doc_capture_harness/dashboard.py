"""Local progress page: `python -m doc_capture_harness dashboard <folder>`.

Serves http://127.0.0.1:<port>/ which polls /status.json. Every folder under
<folder> holding a manifest.json is one run (works for single runs and batches).
The page can pause, resume or stop the capture by creating or deleting the
PAUSE / STOP files in <folder>; the capture checks those files between pages.
Localhost only: the pages show file paths.
"""
from __future__ import annotations

import json
import threading
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
        "paused": bool(d.get("paused")),
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


def status(folder: Path) -> dict:
    return {"paused": (folder / "PAUSE").exists(), "stopping": (folder / "STOP").exists(),
            "runs": collect(folder)}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Capture progress</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>
:root{--bg:#0b0b0b;--ink:#b8b8b8;--bright:#d2d2d2;--dim:#858585;--line:#262626;--accent:#e5484d;
  --mono:'IBM Plex Mono',ui-monospace,Menlo,Consolas,monospace;--text-line:19px;--gap:19px;--gutter:1.5rem}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:400 15px/var(--text-line) var(--mono);-webkit-font-smoothing:antialiased;overflow-wrap:anywhere}
:focus-visible{outline:1px dashed var(--accent);outline-offset:2px}
b{font-weight:600;color:var(--bright)}
button{padding:0;border:0;background:none;color:var(--ink);font:inherit;cursor:pointer}
button:hover,button:focus-visible{color:var(--accent)}
.page{max-width:810px;margin:0 auto;padding:2.5rem var(--gutter) 4rem}
.page>*+*{margin-top:var(--gap)}
.dim{color:var(--dim)}
.accent{color:var(--accent)}
.cmdline{color:var(--bright)}
.ps{margin-right:.1ch;color:var(--accent)}
.controls{display:flex;gap:3ch;flex-wrap:wrap}
.panel{padding:1rem 2ch;border:1px solid var(--line)}
.top{display:flex;justify-content:space-between;gap:2ch;flex-wrap:wrap}
.row{display:grid;grid-template-columns:19ch 1fr;gap:2ch}
.row .key{color:var(--dim)}
.bar{height:8px;border:1px solid var(--line)}
.bar i{display:block;height:100%;background:var(--ink)}
@media(max-width:640px){:root{--gutter:1rem}body{font-size:14px}.row{grid-template-columns:1fr;gap:9px}}
</style></head><body><main class="page">
<p class="cmdline"><span class="ps">&gt;</span> capture progress</p>
<p class="controls"><button id="pause" type="button">pause</button><button id="play" type="button">play</button><button id="stop" type="button">stop</button></p>
<p id="state" class="dim" aria-live="polite">loading…</p>
<div id="runs"></div>
</main>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct=(n,d)=>Math.min(100,Math.round(100*n/(d||1)));
function render(runs){
  return runs.length?runs.map(r=>{
    if(r.error)return `<div class="panel accent">${esc(r.dir)}: ${esc(r.error)}</div>`;
    const tot=r.expected||r.captured||1;
    return `<div class="panel"><div class="top"><b>${esc(r.document)}</b><span class="${r.paused?"accent":"dim"}">${esc(r.status)}${r.paused?" (paused)":""}</span></div>
    <p class="row"><span class="key">captured</span><span>${r.captured}${r.expected?" / "+r.expected:""}</span></p>
    <div class="bar"><i style="width:${pct(r.captured,tot)}%"></i></div>
    <p class="row"><span class="key">ocr</span><span>${r.ocr_done} / ${r.captured}</span></p>
    <div class="bar"><i style="width:${pct(r.ocr_done,r.captured)}%"></i></div>
    <p class="row"><span class="key">problems</span><span class="${r.problems?"accent":""}">${r.problems}</span></p>
    <p class="row"><span class="key">errors</span><span class="${r.errors?"accent":""}">${r.errors}</span></p>
    <p class="row"><span class="key">updated</span><span>${esc(r.updated)}</span></p>
    ${r.pdf?`<p class="row"><span class="key">pdf</span><span>${esc(r.pdf)}</span></p>`:""}
    <p class="dim">${esc(r.dir)}</p></div>`;
  }).join(""):"<p>No runs found yet.</p>";
}
async function tick(){
  try{
    const s=await (await fetch("status.json",{cache:"no-store"})).json();
    document.getElementById("runs").innerHTML=render(s.runs);
    const st=document.getElementById("state");
    st.className=s.stopping||s.paused?"accent":"dim";
    st.textContent=(s.stopping?"stopping":s.paused?"paused — press play to continue":"running")+" · refreshed "+new Date().toLocaleTimeString();
    document.getElementById("pause").hidden=s.paused||s.stopping;
    document.getElementById("play").hidden=!s.paused||s.stopping;
    document.getElementById("stop").hidden=s.stopping;
  }catch(e){document.getElementById("state").textContent="not connected: the capture has finished or its window was closed";}
}
async function control(cmd){
  if(cmd==="stop"&&!confirm("Stop the capture? You can continue later by running it again."))return;
  try{await fetch("/"+cmd,{method:"POST",headers:{"X-Capture-Control":"1"}});}catch(e){}
  tick();
}
for(const c of ["pause","play","stop"])document.getElementById(c).onclick=()=>control(c);
tick();setInterval(tick,2000);
</script></body></html>"""


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False  # HTTPServer turns this on; on Windows that lets two servers share one port


def make_server(folder: Path, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] == "/status.json":
                self._send(200, json.dumps(status(folder)).encode(), "application/json")
            else:
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")

        def do_POST(self):
            path = self.path.split("?")[0]
            if path not in ("/pause", "/play", "/stop"):
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            # Other websites can't add this header without a CORS preflight, which this server never grants.
            if self.headers.get("X-Capture-Control") != "1":
                return self._send(403, b"forbidden", "text/plain; charset=utf-8")
            if path == "/pause":
                (folder / "PAUSE").touch()
            elif path == "/play":
                (folder / "PAUSE").unlink(missing_ok=True)
            else:
                (folder / "STOP").touch()
            self._send(200, json.dumps(status(folder)).encode(), "application/json")

        def log_message(self, *a):
            pass

    return _Server(("127.0.0.1", port), Handler)  # localhost only: shows file paths


def serve(folder: Path, port: int = 8765, open_browser: bool = True) -> None:
    srv = make_server(folder, port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Progress page: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


def start_in_background(folder: Path, port: int = 8765, open_browser: bool = True) -> str:
    try:
        srv = make_server(folder, port)
    except OSError:  # port busy: take any free port instead
        srv = make_server(folder, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    if open_browser:
        webbrowser.open(url)
    return url
