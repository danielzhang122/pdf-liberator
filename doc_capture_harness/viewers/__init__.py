"""Viewer adapters. Windows-only modules are imported lazily."""
from __future__ import annotations

from .base import CaptureSource, ViewerAdapter
from .profiles import PROFILES


def create_adapter(cfg, estop, checkpoint, inputs, ignore_title_re=None) -> ViewerAdapter:
    profile = cfg.profile
    if profile == "acrobat":
        from .acrobat import AcrobatAdapter as cls
    elif profile == "pdfxchange":
        from .pdfxchange import PdfXChangeAdapter as cls
    else:
        from .win32 import Win32ViewerAdapter as cls
    return cls(cfg, estop, checkpoint, inputs, ignore_title_re)


__all__ = ["ViewerAdapter", "CaptureSource", "PROFILES", "create_adapter"]
