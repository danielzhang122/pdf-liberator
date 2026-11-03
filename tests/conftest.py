import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from doc_capture_harness.config import build_config  # noqa: E402

HAS_TESSERACT = shutil.which("tesseract") is not None
needs_tesseract = pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")


def fast_config(doc: Path, **overrides):
    raw = {
        "document": {"path": str(doc), "max_pages": 50},
        "viewer": {"profile": "generic"},
        "capture": {
            "stability": {"interval_s": 0.0, "min_wait_s": 0.0, "timeout_s": 2.0,
                          "required_stable_frames": 3},
            "placeholder_wait_s": 0.0,
            "retries": 2,
        },
        "prompts": {"startup_timeout_s": 3.0},
        "manual": {"mode": "abort", "beep": False},
        "safety": {"estop_hotkey": None},
        "logging": {"console": False},
    }
    from doc_capture_harness.config import deep_merge
    return build_config(deep_merge(raw, overrides))


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "sample document.pdf"
    p.write_bytes(b"%PDF-1.7\n% placeholder bytes for hashing only\n")
    return p
