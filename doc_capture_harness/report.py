"""Verification report (report.json + report.md) built from the manifest."""
from __future__ import annotations

import statistics
from typing import Optional

from .models import EndReason, Flag, INCOMPLETE_FLAGS
from .state import Manifest, RunPaths, atomic_write_json, atomic_write_text, now_iso

CERTAIN_ENDS = {EndReason.KNOWN_PAGE_COUNT.value, EndReason.INDICATOR_TOTAL.value}

END_EXPLANATION = {
    EndReason.KNOWN_PAGE_COUNT.value: "Stopped at the configured known page count.",
    EndReason.INDICATOR_TOTAL.value: "Stopped at the total page count reported by the reader UI.",
    EndReason.MAX_PAGES.value: "Stopped by the max_pages safety limit; later pages may exist.",
    EndReason.STALL_CONFIRMED.value: ("Next-page had no visible effect, and a back/forward probe showed "
                                      "navigation still works. Probably the end, but trailing pages that "
                                      "look identical to the last one cannot be ruled out."),
    EndReason.STALL_UNCONFIRMED.value: ("Next-page had no visible effect and the end could not be confirmed "
                                        "(single page, probe disabled, or unresponsive navigation)."),
    EndReason.NAVIGATION_FAILED.value: "Navigation failed and the operator ended the run.",
    EndReason.USER_ABORT.value: "Operator aborted at a manual checkpoint (resumable).",
    EndReason.EMERGENCY_STOP.value: "Emergency stop (resumable).",
    EndReason.ERROR.value: "Run failed with an error; see logs and diagnostics.",
}


def _pct(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))], 2)


def build_summary(m: Manifest) -> dict:
    d = m.data
    pages = [d["pages"][k] for k in sorted(d["pages"], key=int)]
    captured = [p for p in pages if p.get("status") == "captured"]
    failed = [p for p in pages if p.get("status") != "captured"]
    expected = (d.get("expected_pages") or {}).get("count")
    last_seen = max((p["page"] for p in pages), default=0)
    missing = []
    upper = max(expected or 0, last_seen)
    present = {p["page"] for p in captured}
    missing = [n for n in range(1, upper + 1) if n not in present]

    review = []
    for p in pages:
        flags = list(p.get("flags", [])) + list((p.get("ocr") or {}).get("flags", []))
        bad = sorted(set(f for f in flags if f in INCOMPLETE_FLAGS))
        if p.get("status") != "captured":
            bad = sorted(set(bad) | {Flag.CAPTURE_FAILED})
        if bad:
            review.append({"page": p["page"], "flags": bad})

    confs = [p["ocr"]["mean_conf"] for p in captured if p.get("ocr") and p["ocr"].get("mean_conf") is not None]
    words = [p["ocr"]["words"] for p in captured if p.get("ocr")]
    cap_times = [p.get("timings", {}).get("capture_s", 0) for p in pages]
    nav_times = [p.get("timings", {}).get("navigation_s", 0) for p in pages]
    retries = sum(p.get("retries", 0) for p in pages)
    end = d.get("end") or {}
    integ = d.get("document_integrity") or {}
    return {
        "generated": now_iso(),
        "run_id": d["run_id"],
        "status": d["status"],
        "document": d["document"],
        "viewer": d["viewer"],
        "sessions": len(d.get("sessions", [])),
        "end": end,
        "end_explanation": END_EXPLANATION.get(end.get("reason", ""), ""),
        "end_is_uncertain": d["status"] == "completed" and end.get("reason") not in CERTAIN_ENDS,
        "expected_pages": d.get("expected_pages"),
        "page_count_disagreement": d.get("page_count_disagreement"),
        "pages_captured": len(captured),
        "pages_failed": len(failed),
        "missing_pages": missing,
        "pages_needing_review": review,
        "retries_total": retries,
        "pages_with_retries": [p["page"] for p in pages if p.get("retries")],
        "prompts_handled": len(d["events"].get("prompts_handled", [])),
        "manual_checkpoints": len(d["events"].get("manual_checkpoints", [])),
        "errors": d["events"].get("errors", []),
        "input_fallback_uses": d.get("input_fallback_uses", 0),
        "ocr": {
            "pages_ocrd": len(confs),
            "words_total": sum(words),
            "mean_of_page_mean_conf": round(statistics.mean(confs), 2) if confs else None,
            "min_page_mean_conf": round(min(confs), 2) if confs else None,
            "p10_page_mean_conf": _pct(confs, 0.1),
            "pages_low_confidence": [p["page"] for p in captured
                                     if Flag.LOW_CONFIDENCE in (p.get("ocr") or {}).get("flags", [])],
            "engine": d.get("ocr_engine"),
        },
        "timings": {
            **d.get("timings", {}),
            "capture_per_page_mean_s": round(statistics.mean(cap_times), 3) if cap_times else None,
            "capture_per_page_max_s": round(max(cap_times), 3) if cap_times else None,
            "navigation_per_page_mean_s": round(statistics.mean(nav_times), 3) if nav_times else None,
        },
        "document_unchanged": integ.get("unchanged", True) if integ.get("checked") else True,
        "document_integrity": integ,
        "environment": d.get("environment", {}),
        "pages": [
            {"page": p["page"], "status": p.get("status"), "attempts": p.get("attempts"),
             "flags": p.get("flags", []), "size": p.get("size"), "indicator": p.get("indicator", {}).get("raw"),
             "capture_s": p.get("timings", {}).get("capture_s"),
             "ocr_words": (p.get("ocr") or {}).get("words"),
             "ocr_mean_conf": (p.get("ocr") or {}).get("mean_conf"),
             "ocr_min_conf": (p.get("ocr") or {}).get("min_conf"),
             "ocr_flags": (p.get("ocr") or {}).get("flags", [])}
            for p in pages
        ],
    }


def render_markdown(s: dict) -> str:
    L = []
    a = L.append
    a(f"# Capture verification report — {s['document']['name']}\n")
    a(f"- **Status:** {s['status']}  ")
    a(f"- **Viewer profile:** {s['viewer']}  ")
    a(f"- **Run:** {s['run_id']} ({s['sessions']} session(s)), generated {s['generated']}  ")
    a(f"- **Document SHA-256:** `{s['document']['sha256']}` — "
      f"{'unchanged' if s['document_unchanged'] else '**CHANGED DURING RUN**'}\n")
    a("## End of document\n")
    end = s["end"] or {}
    a(f"- Reason: `{end.get('reason', 'n/a')}` — {end.get('detail', '')}")
    a(f"- {s['end_explanation']}")
    exp = s.get("expected_pages") or {}
    a(f"- Expected pages: {exp.get('count', 'unknown')} (source: {exp.get('source', 'unknown')})")
    if s.get("page_count_disagreement"):
        a(f"- **Page-count disagreement:** {s['page_count_disagreement']}")
    if s["end_is_uncertain"]:
        a("- **End-of-document is heuristic for this run; verify the last page manually or "
          "rerun with `known_page_count`.**")
    a("")
    a("## Summary\n")
    a("| Metric | Value |\n|---|---|")
    rows = [
        ("Pages captured", s["pages_captured"]),
        ("Pages failed", s["pages_failed"]),
        ("Missing pages", ", ".join(map(str, s["missing_pages"])) or "none"),
        ("Pages needing review", len(s["pages_needing_review"])),
        ("Retries (total)", s["retries_total"]),
        ("Prompts handled", s["prompts_handled"]),
        ("Manual checkpoints", s["manual_checkpoints"]),
        ("pyautogui fallback uses", s["input_fallback_uses"]),
        ("OCR words", s["ocr"]["words_total"]),
        ("OCR mean of page-mean confidence", s["ocr"]["mean_of_page_mean_conf"]),
        ("OCR lowest page-mean confidence", s["ocr"]["min_page_mean_conf"]),
        ("Low-confidence pages", ", ".join(map(str, s["ocr"]["pages_low_confidence"])) or "none"),
    ]
    for k, v in rows:
        a(f"| {k} | {v} |")
    a("")
    a("## Timings (seconds)\n")
    a("| Phase | Seconds |\n|---|---|")
    for k, v in s["timings"].items():
        a(f"| {k} | {v} |")
    a("")
    if s["pages_needing_review"]:
        a("## Pages needing review\n")
        a("| Page | Flags |\n|---|---|")
        for r in s["pages_needing_review"]:
            a(f"| {r['page']} | {', '.join(r['flags'])} |")
        a("")
    a("## Per-page detail\n")
    a("| Page | Status | Attempts | Size | Indicator | Capture s | Words | Mean conf | Min conf | Flags |")
    a("|---|---|---|---|---|---|---|---|---|---|")
    for p in s["pages"]:
        size = "x".join(map(str, p["size"])) if p.get("size") else ""
        flags = ", ".join(list(p["flags"]) + list(p["ocr_flags"]))
        a(f"| {p['page']} | {p['status']} | {p['attempts']} | {size} | {p['indicator'] or ''} | "
          f"{p['capture_s']} | {p['ocr_words'] if p['ocr_words'] is not None else ''} | "
          f"{p['ocr_mean_conf'] if p['ocr_mean_conf'] is not None else ''} | "
          f"{p['ocr_min_conf'] if p['ocr_min_conf'] is not None else ''} | {flags} |")
    a("")
    if s["errors"]:
        a("## Errors and failures\n")
        for e in s["errors"]:
            a(f"- {e.get('ts', '')} `{e.get('type', '')}` page {e.get('page', '-')}: {e.get('detail', '')}")
        a("")
    env = s.get("environment") or {}
    if env:
        a("## Environment\n")
        for k in ("os", "dpi_awareness", "viewer_exe", "window_dpi", "document_monitors", "document_pane_found"):
            if k in env:
                a(f"- {k}: {env[k]}")
        for mon in env.get("monitors", []):
            a(f"- monitor {mon['device']}: {mon['rect']} scale {mon['scale']} primary={mon['primary']}")
    a("\n_Flags marked for review mean the capture or OCR may be incomplete; check the PNG in `pages/` "
      "and diagnostics in `diagnostics/`._\n")
    return "\n".join(L)


def write_report(m: Manifest, paths: RunPaths) -> dict:
    s = build_summary(m)
    atomic_write_json(paths.report_json, s)
    atomic_write_text(paths.report_md, render_markdown(s))
    return s
