"""Manual checkpoints: pause and hand control to the operator.

Used whenever the harness cannot proceed safely on its own: an unknown or
security-related dialog, an obstruction over the document, a navigation
failure, a lost focus, an ambiguous window, or a configured setup step.

Modes:
  messagebox  topmost Windows message box  (Abort / Retry / Ignore)
  console     prompt in the terminal       ([r]etry / [c]ontinue / [s]kip / [a]bort)
  abort       unattended: every checkpoint aborts the run (resumable)

Return values: "retry" (re-check the situation), "continue" (operator says it
is resolved; proceed), "skip" (skip the current page; console only).
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from .config import ManualConfig
from .errors import UserAbort
from .estop import EmergencyStopMonitor
from .logging_utils import event, get_logger

HELP = ("\n\nRetry = re-check now.  Ignore/Continue = proceed as if resolved.  "
        "Abort = stop the run (it can be resumed later with the `resume` command).")


class ManualCheckpoint:
    def __init__(self, cfg: ManualConfig, estop: EmergencyStopMonitor,
                 diagnostics: Optional[Callable[[str], dict]] = None,
                 record: Optional[Callable[[dict], None]] = None,
                 responder: Optional[Callable[[str, str], str]] = None):
        self.cfg = cfg
        self.estop = estop
        self.diagnostics = diagnostics
        self.record = record
        self.responder = responder   # injected in tests / embedding
        self.log = get_logger()
        self.count = 0

    def __call__(self, reason: str, message: str) -> str:
        self.estop.check()
        self.count += 1
        diag = {}
        if self.diagnostics:
            try:
                diag = self.diagnostics(f"checkpoint_{reason}") or {}
            except Exception as exc:  # diagnostics must never mask the checkpoint
                diag = {"error": str(exc)}
        event(self.log, "manual_checkpoint", message.splitlines()[0] if message else reason,
              level=30, reason=reason, mode=self.cfg.mode, diagnostics=diag)
        if self.responder is not None:
            answer = self.responder(reason, message)
        elif self.cfg.mode == "abort":
            answer = "abort"
        elif self.cfg.mode == "messagebox" and sys.platform == "win32":
            answer = self._messagebox(reason, message)
        else:
            answer = self._console(reason, message)
        if self.record:
            self.record({"reason": reason, "message": message, "answer": answer, "diagnostics": diag})
        event(self.log, "manual_checkpoint_answer", answer, reason=reason)
        if answer == "abort":
            if self.responder is None and self.cfg.mode == "abort":
                first = (message or "").strip().splitlines()[0] if message else ""
                raise UserAbort(f"needs attention ({reason}): {first}"[:300])
            raise UserAbort(f"operator aborted at checkpoint '{reason}'")
        self.estop.check()
        return answer

    def _messagebox(self, reason: str, message: str) -> str:
        from . import platform_win as pw

        if self.cfg.beep:
            pw.beep()
        flags = 0x2 | 0x30 | 0x40000 | 0x10000   # ABORTRETRYIGNORE | ICONWARNING | TOPMOST | SETFOREGROUND
        res = pw.message_box(message + HELP, f"Capture harness - action needed ({reason})", flags)
        return {3: "abort", 4: "retry", 5: "continue"}.get(res, "retry")

    def _console(self, reason: str, message: str) -> str:
        if self.cfg.beep:
            sys.stderr.write("\a")
        sys.stderr.write(f"\n=== MANUAL CHECKPOINT: {reason} ===\n{message}{HELP}\n")
        while True:
            sys.stderr.write("[r]etry / [c]ontinue / [s]kip page / [a]bort > ")
            sys.stderr.flush()
            line = sys.stdin.readline()
            if not line:  # EOF: no operator available
                return "abort"
            ch = line.strip().lower()[:1]
            mapping = {"r": "retry", "c": "continue", "s": "skip", "a": "abort"}
            if ch in mapping:
                return mapping[ch]
