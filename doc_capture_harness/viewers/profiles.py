"""Built-in viewer profiles: defaults for the ``viewer`` config section.

These are *starting points*. Window classes, control names and shortcuts differ
between reader versions, languages and enterprise policies. Verify each value
with ``python -m doc_capture_harness calibrate`` before relying on it, and
override anything in your config file.

Keys use pywinauto ``send_keys`` syntax: ``^`` Ctrl, ``+`` Shift, ``%`` Alt,
``{RIGHT}``, ``{PGDN}``, ``{HOME}``, ...
"""

# Rules shared by profiles. Note: only *non-security* prompts are handled
# automatically. Anything mentioning JavaScript, security, trust, certificates,
# etc. is routed to a manual checkpoint.
_SAVE_CHANGES_NO = {
    "name": "decline-save-changes-on-close",
    "text_re": r"(?i)save changes",
    "action": "click",
    "button": "No",
    "max_times": 3,
}
# Newer Acrobat shows untitled floating tool strips (quick tools, selection
# toolbar) as separate AVL_AVPopup windows. They are reader chrome, not dialogs;
# when they float over the grey canvas the page crop leaves them out.
_ACROBAT_TOOLBARS = {
    "name": "acrobat-floating-toolbars",
    "class_re": r"^AVL_AVPopup$",
    "title_re": r"^$",
    "action": "ignore",
}
# A go-to-page dialog left open (the harness uses it to move between pages).
_ACROBAT_STRAY_GOTO = {
    "name": "acrobat-stray-go-to-page-dialog",
    "title_re": r"^Go To Page$",
    "action": "click",
    "button": "Cancel",
    "max_times": 100000,
}
_JS_MANUAL = {
    "name": "javascript-prompt-requires-operator",
    "text_re": r"(?i)javascript",
    "action": "manual",
}

PROFILES: dict[str, dict] = {
    "generic": {
        "goto_method": "sequential",
        "_prompt_rules": [],
    },
    "acrobat": {
        "executable_candidates": [
            r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
            r"C:\Program Files (x86)\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
            r"C:\Program Files\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
        ],
        "launch_args": ["{document}"],
        "process_names": ["Acrobat.exe", "AcroRd32.exe"],
        "window_class_re": r"^AcrobatSDIWindow$",
        "window_title_re": r"(?i){stem}",
        # Acrobat renders pages in a Win32 child window of class AVL_AVView
        # whose text is "AVPageView" (Acrobat/Reader DC, 2020-2025 builds).
        "document_pane": {"class_name": "AVL_AVView", "title": "AVPageView", "pick": "largest"},
        "keys": {
            "next_page": "{RIGHT}",
            "prev_page": "{LEFT}",
            "first_page": "{HOME}",
            "last_page": "{END}",
            "goto_page": "^+n",           # Shift+Ctrl+N: Go To Page dialog
            "alt_next_page": ["{PGDN}"],
        },
        "prepare_keys": ["^0"],            # Ctrl+0: zoom to page level (fit page)
        "goto_method": "dialog",
        "goto_dialog_title_re": r"(?i)go to page",
        "navigate_by": "goto",          # page N via Ctrl+Shift+N, not arrow keys
        "overlay_names": ["Ask AI Assistant"],   # label of the AI Assistant bar
        # Page layout has no default shortcut, so it is a manual step.
        "setup_checkpoint": (
            "One-time Acrobat setting (remembered for every document): Edit > Preferences > "
            "Page Display > Page Layout = Single Page, Zoom = Fit Page. Close side panels "
            "that cover the page (they stay closed). Make sure no other Acrobat window "
            "covers the screen."
        ),
        "_prompt_rules": [_ACROBAT_TOOLBARS, _ACROBAT_STRAY_GOTO, _JS_MANUAL, _SAVE_CHANGES_NO],
    },
    "pdfxchange": {
        "executable_candidates": [
            r"C:\Program Files\Tracker Software\PDF Editor\PDFXEdit.exe",
            r"C:\Program Files\Tracker Software\PDF Viewer\PDFXCview.exe",
        ],
        "launch_args": ["{document}"],
        "process_names": ["PDFXEdit.exe", "PDFXCview.exe"],
        "window_title_re": r"(?i){stem}",
        # The PDF-XChange document view class differs between versions; run
        # `calibrate` and set document_pane, or set viewport_margins.
        "keys": {
            "next_page": "{RIGHT}",
            "prev_page": "{LEFT}",
            "first_page": "{HOME}",
            "last_page": "{END}",
            "alt_next_page": ["^{PGDN}"],
        },
        "prepare_keys": [],
        "goto_method": "sequential",
        "setup_checkpoint": (
            "One-time PDF-XChange setting (remembered for every document): Edit > "
            "Preferences > Page Display > Default Page Layout = Single Page and Default "
            "Zoom = Fit Page. Close side panes that cover the page."
        ),
        "_prompt_rules": [_JS_MANUAL, _SAVE_CHANGES_NO],
    },
}


def detect_installed_profile() -> str | None:
    """First profile whose reader executable exists on this machine (Windows)."""
    import os

    for name in ("acrobat", "pdfxchange"):
        for exe in PROFILES[name].get("executable_candidates", []):
            if os.path.exists(os.path.expandvars(exe)):
                return name
    return None
