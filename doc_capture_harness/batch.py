"""Batch processing: many documents, one command, safe to re-run.

    python -m doc_capture_harness batch C:\\Docs              (every PDF under the folder)
    python -m doc_capture_harness batch list.csv             (path[,known_pages][,lang] per row)

For each document the normal harness run is performed (capture -> OCR ->
searchable PDF -> report) and the results are written to the output folder:

    <out>/<same sub-folders>/<name>.ocr.pdf   searchable PDF
    <out>/<same sub-folders>/<name>.ocr.txt   OCR text
    <out>/batch_summary.csv / .md             one row per document
    <out>/_runs/<name>_<id>/                  full run directory (pages, report, logs)

Behaviour:
* Unattended by default: anything needing a person (unknown dialog, failed
  navigation, wrong layout, ...) stops *that document* as "needs_attention"
  (resumable) and the batch moves on. ``--interactive`` pauses instead.
* Re-running the same command skips finished documents (same SHA-256),
  resumes interrupted ones and retries the ones that needed attention.
* The emergency stop (Ctrl+Shift+F12 / STOP file / Ctrl+C) ends the whole batch.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import build_config, deep_merge
from .runner import EXIT_FAILED, EXIT_OK, EXIT_STOPPED, EXIT_WARNINGS, Harness
from .state import Manifest, RunPaths, atomic_write_json, atomic_write_text, now_iso, sha256_file


@dataclass
class BatchItem:
    path: Path
    rel_out: Path                     # output path relative to the batch output folder, without suffix
    known_pages: Optional[int] = None
    lang: Optional[str] = None


def _is_candidate(p: Path) -> bool:
    n = p.name.lower()
    return p.is_file() and n.endswith(".pdf") and not n.endswith(".ocr.pdf") and not n.startswith("~$")


def discover(inputs: list[str], recursive: bool = True, exclude: Optional[Path] = None) -> list[BatchItem]:
    """Expand folders, CSV/TXT lists and single files into batch items."""
    items: list[BatchItem] = []
    excl = exclude.resolve() if exclude else None

    def add(path: Path, rel: Path, known=None, lang=None):
        items.append(BatchItem(path.resolve(), rel.with_suffix(""), known, lang))

    for raw in inputs:
        p = Path(raw.strip().strip('"')).expanduser()
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in sorted(it, key=lambda x: str(x).lower()):
                if excl and (f.resolve() == excl or excl in f.resolve().parents):
                    continue
                if _is_candidate(f):
                    add(f, f.relative_to(p))
        elif p.suffix.lower() == ".csv" and p.is_file():
            with open(p, newline="", encoding="utf-8-sig") as fh:
                rows = csv.DictReader(fh)
                if not rows.fieldnames or "path" not in [c.strip().lower() for c in rows.fieldnames]:
                    raise ValueError(f"{p}: CSV needs a 'path' column (optional: known_pages, lang)")
                for row in rows:
                    row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                    if not row.get("path"):
                        continue
                    f = Path(row["path"])
                    f = f if f.is_absolute() else (p.parent / f)
                    kp = row.get("known_pages")
                    add(f, Path(f.name), int(kp) if kp else None, row.get("lang") or None)
        elif p.suffix.lower() == ".txt" and p.is_file():
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    f = Path(line.strip('"'))
                    f = f if f.is_absolute() else (p.parent / f)
                    add(f, Path(f.name))
        elif p.is_file():
            add(p, Path(p.name))
        else:
            raise FileNotFoundError(f"not found: {p}")
    # de-duplicate, then make output names unique
    seen, unique = set(), []
    for it in items:
        key = str(it.path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(it)
    used: dict[str, int] = {}
    for it in unique:
        k = str(it.rel_out).lower()
        if k in used:
            it.rel_out = it.rel_out.with_name(f"{it.rel_out.name}_{_short(it.path)}")
        used[str(it.rel_out).lower()] = 1
    return unique


def _short(path: Path) -> str:
    return hashlib.sha1(str(path).lower().encode()).hexdigest()[:8]


def _slug(path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem)[:40].strip("._") or "doc"
    return f"{stem}_{_short(path)}"


def default_output_dir(inputs: list[str]) -> Path:
    if len(inputs) == 1:
        p = Path(inputs[0].strip('"')).expanduser().resolve()
        return (p if p.is_dir() else p.parent) / "ocr_output"
    raise ValueError("several inputs given: choose an output folder with --out")


SUMMARY_FIELDS = ["document", "status", "pages", "pages_needing_review", "end_reason", "ocr_mean_conf",
                  "pdf", "text", "minutes", "detail", "report"]


class Batch:
    def __init__(self, raw_cfg: dict, items: list[BatchItem], out_dir: Path,
                 factories: Callable, *, interactive: bool = False,
                 responder: Optional[Callable[[str, str], str]] = None, echo: Callable[[str], None] = print,
                 setup_prompt: Optional[Callable[[str], None]] = None):
        self.raw = raw_cfg
        self.items = items
        self.out = Path(out_dir)
        self.runs = self.out / "_runs"
        self.factories = factories
        self.interactive = interactive
        self.responder = responder
        self.echo = echo
        self.state_path = self.runs / "batch.json"
        self.state = self._load_state()
        self.setup_prompt = setup_prompt

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"created": now_iso(), "docs": {}}

    def _save(self) -> None:
        self.state["updated"] = now_iso()
        atomic_write_json(self.state_path, self.state)
        rows = [d["row"] for d in self.state["docs"].values() if d.get("row")]
        with open(self.out / "batch_summary.csv.tmp", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
            w.writeheader()
            w.writerows(rows)
        Path(self.out / "batch_summary.csv.tmp").replace(self.out / "batch_summary.csv")
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        md = ["# Batch summary", "", f"Updated {self.state['updated']} - output folder `{self.out}`", "",
              "| Status | Documents |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(counts.items())]
        md += ["", "| Document | Status | Pages | Review | End | OCR conf | Minutes | Detail |",
               "|---|---|---|---|---|---|---|---|"]
        for r in rows:
            md.append(f"| {r['document']} | {r['status']} | {r['pages']} | {r['pages_needing_review']} | "
                      f"{r['end_reason']} | {r['ocr_mean_conf']} | {r['minutes']} | {r['detail']} |")
        atomic_write_text(self.out / "batch_summary.md", "\n".join(md) + "\n")

    # ------------------------------------------------------------------ per document
    def _doc_config(self, item: BatchItem, first: bool):
        raw = copy.deepcopy(self.raw)
        pdf_out = self.out / item.rel_out.with_name(item.rel_out.name + ".ocr.pdf")
        txt_out = self.out / item.rel_out.with_name(item.rel_out.name + ".ocr.txt")
        over = {
            "document": {"path": str(item.path)},
            "viewer": {"close_on_finish": True},
            "pdf": {"output_path": str(pdf_out), "overwrite": True},
            "ocr": {"combined_copy_path": str(txt_out)},
        }
        if item.known_pages:
            over["document"]["known_page_count"] = item.known_pages
        if item.lang:
            over["ocr"]["lang"] = item.lang
        raw = deep_merge(raw, over)
        if not self.interactive:
            raw = deep_merge(raw, {"manual": {"mode": "abort"}})
        cfg = build_config(raw)
        # Reader setup is a one-time step: ask once per interactive batch session,
        # never in unattended mode (the layout check catches a wrong view instead).
        if not (self.interactive and first):
            cfg.viewer.setup_checkpoint = None
        return cfg, pdf_out, txt_out

    def _finished(self, st: Optional[dict], sha: str) -> bool:
        return bool(st) and st.get("sha256") == sha and st.get("status") in ("completed", "completed_with_warnings") \
            and Path(st.get("pdf") or "").exists()

    def run(self) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir(parents=True, exist_ok=True)
        stop_file = self.runs / "STOP"
        if stop_file.exists():
            stop_file.unlink()        # stale from a previous stopped batch
        total = len(self.items)
        self.echo(f"Batch: {total} document(s) -> {self.out}"
                  f"{'' if self.interactive else '  (unattended: problems are skipped and listed)'}")
        first = True
        stopped = False
        # One-time reader setup, asked while the operator is still at the screen.
        if self.setup_prompt and not self.interactive and self.items:
            text = build_config(deep_merge(self.raw, {"document": {"path": str(self.items[0].path)}})
                                ).viewer.setup_checkpoint
            if text:
                self.setup_prompt(text)
        for i, item in enumerate(self.items, 1):
            if stop_file.exists():
                stopped = True
                self.echo("Batch stopped (STOP file). Run the same command again to continue.")
                break
            key = _slug(item.path)
            st = self.state["docs"].get(key)
            try:
                sha = sha256_file(item.path)
            except OSError as exc:
                self._record(key, item, "failed", None, None, 0, detail=f"cannot read: {exc}")
                self.echo(f"[{i}/{total}] {item.path.name}: cannot read ({exc})")
                continue
            if self._finished(st, sha):
                self.echo(f"[{i}/{total}] {item.path.name}: already done, skipping")
                continue
            rd = self.runs / key
            resume = (rd / "manifest.json").exists()
            if resume and (not st or st.get("sha256") != sha):
                aside = rd.with_name(f"{rd.name}.replaced-{time.strftime('%Y%m%d-%H%M%S')}")
                shutil.move(str(rd), str(aside))
                resume = False
                self.echo(f"[{i}/{total}] {item.path.name}: document changed since last attempt; starting over "
                          f"(old run kept in {aside.name})")
            self.echo(f"[{i}/{total}] {item.path.name}: {'resuming' if resume else 'starting'}")
            t0 = time.monotonic()
            try:
                cfg, pdf_out, txt_out = self._doc_config(item, first)
                af, sf = self.factories(cfg)
                h = Harness(cfg, rd, af, sf, resume=resume, force=not resume, responder=self.responder,
                            extra_stop_files=(stop_file,))
                code = h.run()
            except Exception as exc:  # noqa: BLE001 - one document must not end the batch
                self._record(key, item, "failed", None, None, time.monotonic() - t0,
                             detail=f"{type(exc).__name__}: {exc}", sha=sha)
                self.echo(f"    failed: {exc}")
                continue
            first = False
            m = Manifest.load(RunPaths(rd)) if (rd / "manifest.json").exists() else None
            mstatus = m.data.get("status") if m else "failed"
            status = {("completed", EXIT_OK): "completed", ("completed", EXIT_WARNINGS): "completed_with_warnings",
                      }.get((mstatus, code)) or {"stopped": "stopped", "aborted": "needs_attention"}.get(mstatus, "failed")
            self._record(key, item, status, m, rd, time.monotonic() - t0, sha=sha,
                         pdf=str(pdf_out) if pdf_out.exists() and status.startswith("completed") else "",
                         txt=str(txt_out) if txt_out.exists() and status.startswith("completed") else "")
            row = self.state["docs"][key]["row"]
            self.echo(f"    {status}: {row['pages']} page(s), {row['pages_needing_review']} to review"
                      f"{', ' + row['detail'] if row['detail'] else ''}")
            if status == "stopped":
                stopped = True
                self.echo("Batch stopped by emergency stop. Run the same command again to continue.")
                break
        self._save()
        rows = [d["row"] for d in self.state["docs"].values() if d.get("row")]
        bad = [r for r in rows if r["status"] not in ("completed",)]
        self.echo(f"\nSummary: {self.out / 'batch_summary.md'}")
        for r in bad:
            self.echo(f"  {r['status']:<24} {r['document']}  {r['detail']}")
        if stopped:
            return EXIT_STOPPED
        if any(r["status"] == "failed" for r in rows):
            return EXIT_FAILED
        return EXIT_WARNINGS if bad else EXIT_OK

    def _record(self, key, item, status, m: Optional[Manifest], rd: Optional[Path], seconds: float, *,
                detail: str = "", sha: str = "", pdf: str = "", txt: str = "") -> None:
        review, end_reason, conf, pages = "", "", "", ""
        if m is not None:
            try:
                rep = json.loads((rd / "report.json").read_text(encoding="utf-8"))
                review = len(rep["pages_needing_review"])
                end_reason = (rep.get("end") or {}).get("reason", "")
                conf = rep["ocr"]["mean_of_page_mean_conf"] or ""
                pages = rep["pages_captured"]
                if not detail and status != "completed":
                    detail = (rep.get("end") or {}).get("detail", "")
                    if status == "completed_with_warnings" and rep.get("end_is_uncertain"):
                        detail = "end of document is heuristic; " + detail
            except (OSError, ValueError, KeyError):
                pass
        prev = self.state["docs"].get(key, {})
        self.state["docs"][key] = {
            "path": str(item.path), "sha256": sha or prev.get("sha256", ""), "status": status,
            "run_dir": str(rd) if rd else prev.get("run_dir", ""), "pdf": pdf, "text": txt,
            "attempts": prev.get("attempts", 0) + 1, "ts": now_iso(),
            "row": {"document": str(item.path), "status": status, "pages": pages, "pages_needing_review": review,
                    "end_reason": end_reason, "ocr_mean_conf": conf, "pdf": pdf, "text": txt,
                    "minutes": round(seconds / 60, 1), "detail": str(detail)[:300].replace("\n", " "),
                    "report": str(rd / "report.md") if rd else ""},
        }
        self._save()


def echo_safe(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode(sys.stdout.encoding or "ascii", "replace").decode(), flush=True)
