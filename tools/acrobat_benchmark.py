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
import re
import subprocess
import sys
import time
from pathlib import Path


def make_pdf(path: Path, pages: int, secured: bool = False) -> None:
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
    if secured:
        # Opens without a password but is encrypted with copy/print/edit restrictions,
        # so Acrobat shows "(SECURED)" in the title bar, like the user's documents.
        w.encrypt(user_password="", owner_password="bench-owner-" + path.stem, permissions_flag=0,
                  algorithm="RC4-128")   # built into pypdf (AES needs the cryptography package)
    with open(path, "wb") as f:
        w.write(f)
    tmp.unlink()


def parse_ts(s: str) -> float:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").timestamp()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--pages", default="20", help="pages per generated document, comma-separated (e.g. 20,11)")
    ap.add_argument("--harness-dir", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--limit", type=float, default=10.0)
    ap.add_argument("--secured", action="store_true", help="encrypt the PDFs (owner password, restrictions)")
    ap.add_argument("--timeout-min", type=float, default=45)
    a = ap.parse_args()
    out = Path(a.outdir).resolve()
    docs = out / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    sizes = [int(x) for x in str(a.pages).split(",") if x.strip()]
    expected = {}
    for i, n in enumerate(sizes):
        name = f"bench{i + 1}_{n}p"
        make_pdf(docs / f"{name}.pdf", n, secured=a.secured)
        expected[name] = n
    results = out / "results"
    cmd = [sys.executable, "-m", "doc_capture_harness", "batch", str(docs), "--out", str(results),
           "--no-prompt", "--max-pages", str(max(sizes) + 5)]
    print("running:", " ".join(cmd), "in", a.harness_dir, flush=True)
    t0 = time.monotonic()
    try:
        rc = subprocess.run(cmd, cwd=a.harness_dir, timeout=a.timeout_min * 60).returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    wall = time.monotonic() - t0

    all_ok = True
    summaries = {}
    for run in sorted((results / "_runs").glob("*")):
        if not (run / "logs" / "harness.jsonl").exists():
            continue
        name = next((k for k in expected if run.name.startswith(k)), run.name)
        ok, summary = report_run(run, expected.get(name), a.limit)
        summaries[name] = summary
        all_ok = all_ok and ok
    missing = [k for k in expected if k not in summaries]
    if missing:
        print("documents not processed:", missing)
        all_ok = False
    (out / "benchmark.json").write_text(json.dumps({"exit_code": rc, "wall_s": round(wall, 1),
                                                     "documents": summaries}, indent=2), encoding="utf-8")
    print("RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


def report_run(run: Path, pages_expected, limit: float):
    events = [json.loads(line) for line in (run / "logs" / "harness.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    prog = [e for e in events if e.get("event") == "progress"]
    start = next((e for e in events if e.get("event") in ("layout", "document_loaded")), None)
    times = [parse_ts(e["ts"]) for e in prog]
    per_page = [round(b - a_, 2) for a_, b in zip(times, times[1:])]
    if times and start:
        per_page.insert(0, round(times[0] - parse_ts(start["ts"]), 2))
    report = json.loads((run / "report.json").read_text(encoding="utf-8")) if (run / "report.json").exists() else {}
    print(f"\n################ {run.name} ################")
    print("=== per-page progress lines ===")
    for e in prog:
        print(e["ts"], e["msg"])
    print("\n=== problems logged ===")
    for e in events:
        if e.get("level") in ("WARNING", "ERROR"):
            print(e["ts"], e.get("event"), e.get("msg"), {k: v for k, v in e.items()
                                                           if k not in ("ts", "level", "event", "msg")})
    print("\n=== navigation attempts ===")
    for e in events:
        if e.get("event") in ("nav_attempt", "end_of_document", "resume_position", "page_count_probe",
                              "goto_unavailable", "goto_dialog_controls", "focus_stolen"):
            print(e["ts"], json.dumps({k: v for k, v in e.items() if k not in ("ts", "level")}, default=str))
    print("\n=== diagnostics ===")
    for f in sorted((run / "diagnostics").glob("*.json")):
        print(f.name, f.read_text(encoding="utf-8")[:2500])
    avg = sum(per_page) / len(per_page) if per_page else None
    pages_ok = verify_pages(run, pages_expected)
    summary = {"pages_expected": pages_expected, "pages_captured": report.get("pages_captured"),
               "pages_in_order": pages_ok, "status": report.get("status"),
               "end": (report.get("end") or {}).get("reason"), "end_detail": (report.get("end") or {}).get("detail"),
               "seconds_per_page": per_page, "avg_s_per_page": round(avg, 2) if avg else None,
               "max_s_per_page": max(per_page) if per_page else None,
               "capture_breakdown_s": (report.get("timings") or {}).get("capture_breakdown_s")}
    print("\n=== summary ===\n" + json.dumps(summary, indent=2))
    ok = (avg is not None and avg <= limit and report.get("pages_captured") == pages_expected
          and report.get("status") == "completed" and pages_ok)
    return ok, summary


def verify_pages(run: Path, pages_expected) -> bool:
    """OCR text of saved page N must say 'Benchmark page N' (no skipped/duplicated pages)."""
    ocr = run / "ocr"
    if not pages_expected:
        return False
    bad = []
    for n in range(1, pages_expected + 1):
        f = ocr / f"page_{n:04d}.txt"
        text = f.read_text(encoding="utf-8") if f.exists() else ""
        m = re.search(r"benchmark\s+page\s+(\d+)", text, re.I)
        if not m or int(m.group(1)) != n:
            bad.append(n)
    if bad:
        print("pages whose OCR does not show the expected page number:", bad)
    return not bad


if __name__ == "__main__":
    sys.exit(main())
