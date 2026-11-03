"""Run directory layout and the resumable manifest.

Layout of a run directory::

    <run>/
      manifest.json          # run state (header); page records live in records/
      records/page_0001.json # one atomic record per page (authoritative for resume)
      config.resolved.json   # the effective configuration
      pages/page_0001.png    # cropped captures
      slices/                # raw scroll slices (optional)
      ocr/page_0001.txt      # per-page OCR text (+ .tsv word data)
      ocr/pdf/page_0001.pdf  # per-page searchable PDF from the same Tesseract pass
      ocr/combined.txt
      diagnostics/           # screenshots + JSON captured on failures
      logs/harness.jsonl
      report.json, report.md
      work/                  # read-only working copy of the document (open_copy)
      STOP                   # create this file to request an emergency stop
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

MANIFEST_VERSION = 1


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, default=str, ensure_ascii=False))


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class RunPaths:
    root: Path

    @property
    def manifest(self) -> Path: return self.root / "manifest.json"
    @property
    def pages(self) -> Path: return self.root / "pages"
    @property
    def slices(self) -> Path: return self.root / "slices"
    @property
    def ocr(self) -> Path: return self.root / "ocr"
    @property
    def diagnostics(self) -> Path: return self.root / "diagnostics"
    @property
    def logs(self) -> Path: return self.root / "logs"
    @property
    def work(self) -> Path: return self.root / "work"
    @property
    def records(self) -> Path: return self.root / "records"
    @property
    def page_pdfs(self) -> Path: return self.root / "ocr" / "pdf"

    def record(self, n: int) -> Path: return self.records / f"page_{n:04d}.json"
    def page_pdf(self, n: int) -> Path: return self.page_pdfs / f"page_{n:04d}.pdf"
    @property
    def report_json(self) -> Path: return self.root / "report.json"
    @property
    def report_md(self) -> Path: return self.root / "report.md"

    def page_png(self, n: int) -> Path: return self.pages / f"page_{n:04d}.png"
    def page_txt(self, n: int) -> Path: return self.ocr / f"page_{n:04d}.txt"
    def page_tsv(self, n: int) -> Path: return self.ocr / f"page_{n:04d}.tsv"

    def ensure(self) -> None:
        for d in (self.root, self.pages, self.ocr, self.diagnostics, self.logs, self.records):
            d.mkdir(parents=True, exist_ok=True)


class Manifest:
    """Run state with atomic, scale-friendly persistence.

    Page records live in ``records/page_NNNN.json`` (one small atomic write per
    page, so cost does not grow with document length). ``manifest.json`` holds
    everything else and is rewritten at most every ``header_interval_s``
    seconds, plus on :meth:`save` / :meth:`finalize`. On load, page records
    from ``records/`` are authoritative, so a crash loses at most a few seconds
    of header bookkeeping and never a captured page.
    """

    header_interval_s = 5.0

    def __init__(self, paths: RunPaths, data: dict):
        self.paths = paths
        self.data = data
        self._last_header = 0.0

    # ------------------------------------------------------------------ create/load
    @classmethod
    def create(cls, paths: RunPaths, *, document: dict, viewer: str, config_fingerprint: str,
               run_id: str) -> "Manifest":
        data = {
            "version": MANIFEST_VERSION,
            "run_id": run_id,
            "created": now_iso(),
            "updated": now_iso(),
            "status": "running",
            "document": document,
            "viewer": viewer,
            "config_fingerprint": config_fingerprint,
            "sessions": [],
            "pages": {},
            "end": None,
            "environment": {},
            "timings": {},
            "events": {"prompts_handled": [], "manual_checkpoints": [], "errors": []},
        }
        m = cls(paths, data)
        m.save()
        return m

    @classmethod
    def load(cls, paths: RunPaths) -> "Manifest":
        data = json.loads(paths.manifest.read_text(encoding="utf-8"))
        if data.get("version") != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version {data.get('version')}")
        pages = dict(data.get("pages") or {})
        if paths.records.is_dir():
            for f in paths.records.glob("page_*.json"):
                try:
                    rec = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue          # a torn write cannot happen (atomic), but be safe
                pages[str(int(rec["page"]))] = rec
        data["pages"] = pages
        return cls(paths, data)

    def save(self, include_pages: bool = False) -> None:
        """Write the header now. ``include_pages`` also embeds all page records
        (done once at the end so manifest.json is self-contained)."""
        self.data["updated"] = now_iso()
        header = {k: v for k, v in self.data.items() if k != "pages"}
        header["pages"] = self.data["pages"] if include_pages else {}
        header["page_records"] = "records/"
        atomic_write_json(self.paths.manifest, header)
        self._last_header = time.monotonic()

    def save_soon(self) -> None:
        if time.monotonic() - self._last_header >= self.header_interval_s:
            self.save()

    def finalize(self) -> None:
        self.save(include_pages=True)

    # ------------------------------------------------------------------ pages
    def page(self, n: int) -> Optional[dict]:
        return self.data["pages"].get(str(n))

    def set_page(self, n: int, record: dict) -> None:
        record["page"] = n
        self.data["pages"][str(n)] = record
        atomic_write_json(self.paths.record(n), record)
        self.save_soon()

    def page_numbers(self) -> list[int]:
        return sorted(int(k) for k in self.data["pages"])

    def captured_pages(self) -> list[int]:
        return [n for n in self.page_numbers() if self.data["pages"][str(n)].get("status") == "captured"]

    def next_page_to_capture(self, start_page: int = 1) -> int:
        """First page >= start_page that is not captured (contiguous resume)."""
        n = start_page
        captured = set(self.captured_pages())
        while n in captured:
            n += 1
        return n

    def add_event(self, kind: str, payload: dict) -> None:
        self.data["events"].setdefault(kind, []).append({"ts": now_iso(), **payload})
        self.save_soon()
