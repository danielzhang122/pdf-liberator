"""Real-reader benchmark: capture a generated PDF with the harness in Acrobat
and report seconds per page, taken from the harness's own log.

    python tools/acrobat_benchmark.py OUTDIR [--pages 20] [--harness-dir DIR] [--limit 10]

The PDF is generated here (text pages, initial view Single Page + Fit Page),
so the benchmark needs no external documents. Exit code 1 if the average
time per page exceeds --limit or the document was not captured completely.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path


def make_pdf(path: Path, pages: int) -> None:
    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, NameObject

    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 30)
        big = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 64)
    except OSError:
        font = big = ImageFont.load_default()
    words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
             "november oscar papa quebec romeo sierra tango uniform victor whiskey").split()
    imgs = []
    for n in range(1, pages + 1):
        img = Image.new("RGB", (1700, 2200), "white")
        d = ImageDraw.Draw(img)
        d.text((150, 150), f"Benchmark page {n}", fill="black", font=big)
        for i in range(40):
            line = " ".join(words[(n * 7 + i * 3 + k) % len(words)] for k in range(9))
            d.text((150, 300 + i * 45), f"{i + 1}. {line}", fill="black", font=font)
        imgs.append(img)
    tmp = path.with_suffix(".tmp.pdf")
    imgs[0].save(tmp, "PDF", resolution=200.0, save_all=True, append_images=imgs[1:])
    w = PdfWriter(clone_from=PdfReader(str(tmp)))
    w._root_object[NameObject("/PageLayout")] = NameObject("/SinglePage")
    w._root_object[NameObject("/OpenAction")] = ArrayObject([w.pages[0].indirect_reference, NameObject("/Fit")])
    with open(path, "wb") as f:
        w.write(f)
    tmp.unlink()


def parse_ts(s: str) -> float:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").timestamp()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--pages", type=int, default=20)
    ap.add_argument("--harness-dir", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--limit", type=float, default=10.0)
    ap.add_argument("--timeout-min", type=float, default=45)
    a = ap.parse_args()
    out = Path(a.outdir).resolve()
    docs = out / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    pdf = docs / "benchmark.pdf"
    make_pdf(pdf, a.pages)
    results = out / "results"
    cmd = [sys.executable, "-m", "doc_capture_harness", "batch", str(docs), "--out", str(results),
           "--no-prompt", "--max-pages", str(a.pages + 5)]
    print("running:", " ".join(cmd), "in", a.harness_dir, flush=True)
    t0 = time.monotonic()
    try:
        rc = subprocess.run(cmd, cwd=a.harness_dir, timeout=a.timeout_min * 60).returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    wall = time.monotonic() - t0

    logs = list((results / "_runs").glob("*/logs/harness.jsonl"))
    events = [json.loads(line) for f in logs for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    prog = [e for e in events if e.get("event") == "progress"]
    start = next((e for e in events if e.get("event") in ("layout", "document_loaded")), None)
    times = [parse_ts(e["ts"]) for e in prog]
    per_page = [round(b - a_, 2) for a_, b in zip(times, times[1:])]
    if times and start:
        per_page.insert(0, round(times[0] - parse_ts(start["ts"]), 2))
    report = {}
    for f in (results / "_runs").glob("*/report.json"):
        report = json.loads(f.read_text(encoding="utf-8"))
    print("\n=== per-page progress lines ===")
    for e in prog:
        print(e["ts"], e["msg"])
    print("\n=== problems logged ===")
    for e in events:
        if e.get("level") in ("WARNING", "ERROR"):
            print(e["ts"], e.get("event"), e.get("msg"), {k: v for k, v in e.items()
                                                           if k not in ("ts", "level", "event", "msg")})
    print("\n=== navigation attempts (key presses and what had focus) ===")
    for e in events:
        if e.get("event") in ("nav_attempt", "end_of_document", "resume_position"):
            print(e["ts"], json.dumps({k: v for k, v in e.items() if k not in ("ts", "level")}, default=str))
    print("\n=== diagnostics ===")
    for f in sorted((results / "_runs").glob("*/diagnostics/*.json")):
        print(f.name, f.read_text(encoding="utf-8")[:3000])
    avg = sum(per_page) / len(per_page) if per_page else None
    summary = {"exit_code": rc, "wall_s": round(wall, 1), "pages_expected": a.pages,
               "pages_captured": report.get("pages_captured"), "status": report.get("status"),
               "end": (report.get("end") or {}).get("reason"),
               "seconds_per_page": per_page, "avg_s_per_page": round(avg, 2) if avg else None,
               "max_s_per_page": max(per_page) if per_page else None,
               "capture_breakdown_s": (report.get("timings") or {}).get("capture_breakdown_s")}
    (out / "benchmark.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\n=== summary ===\n" + json.dumps(summary, indent=2))
    ok = (avg is not None and avg <= a.limit and report.get("pages_captured") == a.pages
          and report.get("status") == "completed")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
