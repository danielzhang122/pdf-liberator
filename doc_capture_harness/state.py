"""Run directory layout and the resumable manifest.

Layout of a run directory::

    <run>/
      manifest.json          # authoritative state, rewritten atomically after each page
      config.resolved.json   # the effective configuration
      pages/page_0001.png    # cropped captures
      slices/                # raw scroll slices (optional)
      ocr/page_0001.txt      # per-page OCR text (+ .tsv word data)
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
    def report_json(self) -> Path: return self.root / "report.json"
    @property
    def report_md(self) -> Path: return self.root / "report.md"

    def page_png(self, n: int) -> Path: return self.pages / f"page_{n:04d}.png"
    def page_txt(self, n: int) -> Path: return self.ocr / f"page_{n:04d}.txt"
    def page_tsv(self, n: int) -> Path: return self.ocr / f"page_{n:04d}.tsv"

    def ensure(self) -> None:
        for d in (self.root, self.pages, self.ocr, self.diagnostics, self.logs):
            d.mkdir(parents=True, exist_ok=True)


class Manifest:
    """Thin wrapper around the manifest dict with atomic persistence."""

    def __init__(self, paths: RunPaths, data: dict):
        self.paths = paths
        self.data = data

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
        return cls(paths, data)

    def save(self) -> None:
        self.data["updated"] = now_iso()
        atomic_write_json(self.paths.manifest, self.data)

    # ------------------------------------------------------------------ pages
    def page(self, n: int) -> Optional[dict]:
        return self.data["pages"].get(str(n))

    def set_page(self, n: int, record: dict) -> None:
        self.data["pages"][str(n)] = record
        self.save()

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
        self.save()
