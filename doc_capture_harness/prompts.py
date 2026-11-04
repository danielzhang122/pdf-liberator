"""Viewer prompt / dialog handling.

Policy (deliberately conservative):

* A dialog is matched against ``prompts.rules`` in order. The first rule
  whose given patterns (title_re, text_re, class_re) all match applies.
* ``click`` / ``keys`` rules act automatically, **except** when the dialog text
  matches ``security_text_re`` (JavaScript, trust, certificate, password,
  permission, ...). Those require ``acknowledge_security: true`` on the rule,
  i.e. an explicit, per-prompt decision written into your config.
* Unknown dialogs are never dismissed: they trigger a manual checkpoint (or
  abort the run when ``unknown_action: abort``).
* A window of another application covering the document viewport is an
  obstruction and also triggers a manual checkpoint.
* Each rule may act at most ``max_times`` times per run; beyond that the
  harness assumes it is looping and asks the operator.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import PromptRule, PromptsConfig
from .errors import UserAbort
from .estop import EmergencyStopMonitor
from .logging_utils import event, get_logger
from .models import DialogInfo, Rect


def rule_matches(rule: PromptRule, d: DialogInfo) -> bool:
    if rule.title_re and not re.search(rule.title_re, d.title or ""):
        return False
    if rule.text_re and not re.search(rule.text_re, d.all_text()):
        return False
    if rule.class_re and not re.search(rule.class_re, d.class_name or ""):
        return False
    return True


def looks_security_related(d: DialogInfo, patterns: list[str]) -> bool:
    text = d.all_text()
    return any(re.search(p, text) for p in patterns)


@dataclass
class PromptStats:
    handled: list[dict] = field(default_factory=list)

    def count(self) -> int:
        return len(self.handled)


class PromptHandler:
    def __init__(self, cfg: PromptsConfig, adapter, checkpoint: Callable[[str, str], str],
                 estop: EmergencyStopMonitor, record: Optional[Callable[[dict], None]] = None):
        self.cfg = cfg
        self.adapter = adapter
        self.checkpoint = checkpoint
        self.estop = estop
        self.record = record
        self.log = get_logger()
        self.rule_counts: dict[str, int] = {}
        self.accepted_handles: set[int] = set()    # operator chose "continue" / rule says ignore
        self.stats = PromptStats()

    def _note(self, d: DialogInfo, action: str, rule: Optional[str], **extra) -> None:
        rec = {"title": d.title, "class": d.class_name, "action": action, "rule": rule,
               "owned_by_viewer": d.owned_by_viewer, "texts": d.texts[:5], "buttons": d.buttons, **extra}
        self.stats.handled.append(rec)
        event(self.log, "prompt_handled", f"{action}: {d.title!r}", **{k: v for k, v in rec.items() if k != "texts"})
        if self.record:
            self.record(rec)

    def _manual(self, d: DialogInfo, why: str) -> str:
        msg = (f"{why}\n\nWindow title: {d.title!r}\nClass: {d.class_name}\n"
               f"Text: {' / '.join(d.texts[:6])}\nButtons: {', '.join(d.buttons)}\n\n"
               "Handle it yourself in the reader if appropriate (the harness will not "
               "dismiss it), then choose Retry.")
        answer = self.checkpoint("dialog", msg)
        if answer in ("continue", "skip"):
            self.accepted_handles.add(d.handle)
        return answer

    def _rule_for(self, d: DialogInfo) -> Optional[PromptRule]:
        scopes = ("viewer", "any") if d.owned_by_viewer else ("other", "any")
        return next((r for r in self.cfg.rules if r.scope in scopes and rule_matches(r, d)), None)

    def handle(self, doc_rect: Optional[Rect], max_rounds: int = 25) -> int:
        """Process dialogs until none remain that need action. Returns the number of
        dialogs dealt with (including windows that were waited out), 0 if there were none."""
        actions = 0
        rounds = 0
        first_seen: dict[int, float] = {}       # obstruction handle -> when first seen
        waited = False
        while rounds < max_rounds:
            self.estop.check()
            dialogs = [d for d in self.adapter.list_dialogs(doc_rect) if d.handle not in self.accepted_handles]
            if not dialogs:
                # A window that was waited out also counts: the view may have changed.
                return actions + int(waited)
            d = dialogs[0]
            rule = self._rule_for(d)
            if not d.owned_by_viewer and rule is None:
                # Another application's window over the page. Most are transient
                # notifications: give it time to go away before involving anyone.
                if d.handle not in first_seen:
                    first_seen[d.handle] = time.monotonic()
                    if self.cfg.obstruction_wait_s > 0:
                        self._note(d, "obstruction_wait", None, wait_s=self.cfg.obstruction_wait_s)
                if time.monotonic() - first_seen[d.handle] < self.cfg.obstruction_wait_s:
                    waited = True
                    self.estop.sleep(1.0)
                    continue
                rounds += 1
                actions += 1
                self._note(d, "obstruction", None)
                if self.cfg.obstruction_action == "abort":
                    raise UserAbort(f"window {d.title!r} obstructs the document viewport")
                self.checkpoint("obstruction",
                                f"Another window ({d.title!r}, class {d.class_name}) stayed over the document "
                                f"area for {self.cfg.obstruction_wait_s:.0f}s and would be captured. Move or close "
                                "it, or add a prompts rule with scope: other for it, then choose Retry.")
                first_seen.pop(d.handle, None)
                continue
            rounds += 1
            actions += 1
            if rule is None:
                self._note(d, "unknown", None)
                if self.cfg.unknown_action == "abort":
                    raise UserAbort(f"unknown dialog {d.title!r} (prompts.unknown_action=abort)")
                self._manual(d, "An unfamiliar dialog appeared in the reader.")
                continue
            name = rule.name or rule.text_re or rule.title_re or rule.class_re or "rule"
            n = self.rule_counts.get(name, 0) + 1
            self.rule_counts[name] = n
            if rule.action == "ignore":
                self.accepted_handles.add(d.handle)
                self._note(d, "ignore", name)
                continue
            if rule.action == "abort":
                self._note(d, "abort", name)
                raise UserAbort(f"dialog {d.title!r} matched abort rule {name!r}")
            if rule.action == "manual":
                self._note(d, "manual", name)
                self._manual(d, f"Dialog matched rule '{name}', which requires the operator.")
                continue
            if n > rule.max_times:
                self._note(d, "manual_max_times", name, count=n)
                self._manual(d, f"Rule '{name}' already acted {rule.max_times} times; it may be looping.")
                continue
            if rule.action == "wait":
                self._note(d, "wait", name, wait_s=rule.wait_s)
                self.estop.sleep(rule.wait_s)
                continue
            if looks_security_related(d, self.cfg.security_text_re) and not rule.acknowledge_security:
                self._note(d, "manual_security", name)
                self._manual(d, f"Rule '{name}' would act automatically, but the dialog looks "
                                "security-related and the rule does not set acknowledge_security.")
                continue
            ok = False
            if rule.action == "click":
                ok = self.adapter.click_dialog_button(d, rule.button)
            elif rule.action == "keys":
                ok = self.adapter.send_dialog_keys(d, rule.keys)
            self._note(d, rule.action, name, ok=ok, button=rule.button, keys=rule.keys)
            if not ok:
                self._manual(d, f"Rule '{name}' could not perform '{rule.action}'.")
            else:
                self.estop.sleep(0.3)
        self.checkpoint("dialog_loop", "Dialogs keep appearing; please check the reader, then Retry.")
        return actions
