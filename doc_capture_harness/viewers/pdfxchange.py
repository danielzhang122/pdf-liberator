"""PDF-XChange Editor / Viewer adapter.

PDF-XChange's document view class name varies between versions, so the
profile does not ship a ``document_pane`` selector: run ``calibrate`` and copy
the suggested selector into your config (or set ``viewport_margins``).
"""
from __future__ import annotations

from .win32 import Win32ViewerAdapter


class PdfXChangeAdapter(Win32ViewerAdapter):
    name = "pdfxchange"
