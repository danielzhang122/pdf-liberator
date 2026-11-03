"""Adobe Acrobat / Acrobat Reader adapter.

Differences from the generic adapter are mostly configuration (see
``profiles.py``). The override here gives keyboard focus to the AVPageView
pane before navigation, because arrow keys go to whichever Acrobat control
has focus (e.g. the search box would swallow {RIGHT}).

Not automated on purpose:
* Enabling JavaScript, trusting a document, Protected Mode / Protected View
  and any other security prompt: they always go to a manual checkpoint unless
  a rule in *your* config explicitly acknowledges them.
* Page layout (Single Page view) has no default shortcut; see the profile's
  ``setup_checkpoint``.
"""
from __future__ import annotations

from .win32 import Win32ViewerAdapter


class AcrobatAdapter(Win32ViewerAdapter):
    name = "acrobat"

    def send_keys(self, keys: str) -> None:
        self._ensure_focus()
        try:
            self.focus_document()
        except Exception:
            pass
        self.inputs.send_keys(keys)
