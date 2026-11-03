"""Structured logging: JSON Lines to a file, readable text to the console.

Use :func:`event` so every record carries an ``event`` name plus arbitrary
key/value fields, e.g. ``event(log, "page_captured", page=3, attempts=1)``.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

LOGGER_NAME = "doc_capture_harness"


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "event": getattr(record, "event", None) or "message",
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None) or {}
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        base = f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.levelname:<7} {record.getMessage()}"
        out = f"{base}  [{extra}]" if extra else base
        if record.exc_info:
            out += "\n" + self.formatException(record.exc_info)
        return out


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    fh = logging.FileHandler(log_dir / "harness.jsonl", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(JsonLinesFormatter())
    logger.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(getattr(logging, level.upper(), logging.INFO))
        ch.setFormatter(ConsoleFormatter())
        logger.addHandler(ch)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def event(logger: logging.Logger, name: str, msg: str = "", level: int = logging.INFO, **fields) -> None:
    logger.log(level, msg or name, extra={"event": name, "fields": fields})
