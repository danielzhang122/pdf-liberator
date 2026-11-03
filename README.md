# doc_capture_harness

A Windows test harness that opens a document in a **desktop PDF reader** (Adobe Acrobat / Acrobat Reader, PDF-XChange, or a generic configured reader), captures every rendered page as PNG, runs **Tesseract OCR** locally, and writes a **verification report**.

It also assembles the captures into a **searchable PDF**: each page is the captured image with an invisible OCR text layer, sized like the original's pages. The original file is never modified.

It is meant for documents whose real content only appears after the reader runs a viewer-specific wrapper or document JavaScript, so a PDF library sees only a placeholder page.

> **Scope.** Use it only on documents you own or are authorised to test. The harness captures what the reader shows on screen and nothing more. It does **not** decrypt files, bypass DRM, enable JavaScript globally, or dismiss security prompts. If a reader window asks Windows to exclude it from capture (display affinity), the run stops with `CaptureBlocked`.

---

## Contents

1. [How it works](#how-it-works)
2. [Installation](#installation)
3. [Tesseract setup](#tesseract-setup)
4. [Preparing the reader and desktop](#preparing-the-reader-and-desktop)
5. [Configuration](#configuration)
6. [CLI usage](#cli-usage)
7. [Outputs and exit codes](#outputs-and-exit-codes)
8. [End-of-document detection](#end-of-document-detection)
9. [Reliability mechanisms](#reliability-mechanisms)
10. [Manual checkpoints](#manual-checkpoints)
11. [Supporting another reader](#supporting-another-reader)
12. [Assumptions and limitations](#assumptions-and-limitations)
13. [Testing](#testing)

---

## How it works

```
cli.py ──► runner.Harness ──────────────────────────────────────────────┐
              │ open + wait for load   (stability.py, prompts.py)        │
              │ capture page loop      (imaging.py: stable frames,       │  reader-agnostic
              │                         crop, stitch, duplicates)        │
              │ navigate / end detect  (navigation.py)                   │
              │ OCR                    (ocr.py → pytesseract)            │
              │ searchable PDF         (pdf_output.py → Tesseract + pypdf)│
              │ manifest + report      (state.py, report.py)             │
              ▼                                                          ┘
        ViewerAdapter (viewers/)                  CaptureSource
          win32.py      generic pywinauto/ctypes    MssCaptureSource (screen)
          acrobat.py    Acrobat overrides           PrintWindowCaptureSource
          pdfxchange.py PDF-XChange overrides
          profiles.py   per-reader defaults (keys, window/pane selectors, prompts)
```

Reader-specific code is limited to `viewers/`. It covers launching, finding the window and document pane, sending keys, reading the page indicator, busy detection, and dialog enumeration. Stability checks, cropping, stitching, OCR, resume and reporting are shared by every reader. A simulated reader in `tests/fakes.py` uses the same interface, so the whole pipeline is exercised in CI without Windows.

Per page, the runner does the following:

1. It handles pending dialogs and checks that no other window covers the document area.
2. It parks the mouse pointer off the page, then captures the viewport repeatedly. It continues once N consecutive frames are unchanged and the reader is not busy (busy means a wait cursor, a hung window, or a "Loading" title).
3. If a frame is all black, still shows placeholder text, or never stabilises, it retries a bounded number of times. Retries are recorded.
4. In `scroll` mode it scrolls down and stitches overlapping slices until the view stops moving, or until the page indicator shows the next page.
5. It crops the frame to the page using the reader's canvas colour, saves `pages/page_NNNN.png`, and updates `manifest.json` atomically.
6. It advances to the next page, verifies the move, and decides whether the document has ended (see [End-of-document detection](#end-of-document-detection)).

OCR runs after capture, then the searchable PDF is built. Both can also be run later with the `ocr` and `pdf` commands, on any OS.

### Searchable PDF ("merging back" into a PDF)

`pdf_output.py` turns `pages/page_NNNN.png` into `output/<stem>.ocr.pdf`:

- **One PDF page per captured page.** Tesseract's PDF renderer produces the page image with an invisible text layer positioned over each word, so the PDF can be searched, and text selected and copied. It uses the same `ocr.lang`/`psm`/`oem` and resolution as the `.txt` files.
- **Page sizes and metadata come from the original** (`pdf.page_size: original`) when its file can be read *without decryption*: each output page gets the original page's width (height follows the capture), and title/author are copied. Encrypted originals are not decrypted. Pages then use `pdf.fallback_page_size` (letter or a4), and the report says so. Wrapper documents often hold only a placeholder page, so the original's page count may differ; this is noted, not treated as an error.
- **Missing or failed pages** get a red-bordered placeholder page ("Page N was not captured"), so PDF page numbers match document page numbers. Set `pdf.missing_pages: skip` to omit them.
- **Never written over the original.** The default output is inside the run directory. `--next-to-original` (or `pdf.copy_next_to_original`) also writes a *new* `<stem>.ocr.pdf` beside the original. Existing files outside the run directory are only replaced with `--overwrite`, and any path equal to the original or its working copy is refused.
- **Verification.** The written PDF is reopened to check the page count, and that every page where OCR found words has extractable text. Results appear in `report.md` ("Searchable PDF") and affect the exit code.
- **Provenance.** Metadata records the source document's SHA-256, the run id, and a note that the pages are screen captures and the text is OCR.
- Without Tesseract, an image-only PDF is produced and the report notes it.
- The PDF is a derived copy of the rendered content: an image of each page plus OCR text, not the original PDF objects. Fonts, vector graphics, links, form fields and document JavaScript are not preserved. Handle and distribute it under the same terms as the source document.

---

## Installation

Requirements: Windows 10 or 11, Python 3.10+ (64-bit), an interactive desktop session, and the reader installed.

```powershell
py -3.12 -m venv .venv
```

```powershell
.venv\Scripts\Activate.ps1
```

```powershell
python -m pip install --upgrade pip
```

```powershell
python -m pip install -r requirements.txt
```

The individual packages, if you are not using `requirements.txt`:

```powershell
python -m pip install pywinauto pywin32 pyautogui mss Pillow numpy pytesseract PyYAML pypdf pytest
```

If `import win32api` fails after installing pywin32, run its post-install step once from an elevated prompt:

```powershell
python .venv\Scripts\pywin32_postinstall.py -install
```

## Tesseract setup

pytesseract is only a wrapper; the Tesseract engine has to be installed separately.

1. Install Tesseract 5.x for Windows. The UB Mannheim build is the usual choice (<https://github.com/UB-Mannheim/tesseract/wiki>), or use winget:

   ```powershell
   winget install --id UB-Mannheim.TesseractOCR
   ```

2. Default install path: `C:\Program Files\Tesseract-OCR\tesseract.exe`. The harness checks `ocr.tesseract_cmd`, then `PATH`, then the standard install locations.
3. Extra languages: tick them in the installer, or copy `<lang>.traineddata` (from <https://github.com/tesseract-ocr/tessdata_best> or `tessdata_fast`) into `C:\Program Files\Tesseract-OCR\tessdata`. Then set `ocr.lang: eng+deu`.
4. Verify the install:

   ```powershell
   & "C:\Program Files\Tesseract-OCR\tesseract.exe" --version
   ```

   ```powershell
   & "C:\Program Files\Tesseract-OCR\tesseract.exe" --list-langs
   ```

All OCR is local. pytesseract runs the local executable, and no network calls are made.

## Preparing the reader and desktop

These steps depend on the reader. Some cannot be automated reliably, so the profiles add a **setup checkpoint** after the document opens.

| Step | Why | Automated? |
|---|---|---|
| Use **Single Page** layout, not continuous | Each "next page" should show exactly one page, starting at its top | No. Acrobat and PDF-XChange have no default shortcut, so the setup checkpoint asks you to do it |
| Zoom: **fit page** (`fit_page` mode) or **fit width** (`scroll` mode) | Fit page gives one capture per page; fit width gives larger text for OCR | Partly. `prepare_keys` (Acrobat `Ctrl+0`) |
| Close side panels, banners and tool panes that overlap the page | They would be captured or would shrink the page | No (setup checkpoint) |
| Keep the reader on **one monitor**, ideally maximised | Avoids mixed-DPI rectangles | Checked: a manual checkpoint fires if the document area spans monitors with different scaling or is off-screen |
| Keep the session **unlocked**, with no screensaver or RDP minimisation | A locked or minimised session renders black frames or nothing | Detected (`all_black_capture`), not fixable |
| Grant **only** the JavaScript/trust permissions your test plan requires, through the reader's own UI or policy | The harness never enables JavaScript globally | Manual. Prompts mentioning JavaScript always go to the operator |

Larger zoom on a large or portrait monitor improves OCR. Pages with text under about 10 px x-height are flagged `text_too_small_for_reliable_ocr`.

---

## Configuration

Print the annotated example and edit it:

```powershell
python -m doc_capture_harness init-config --viewer acrobat > my-test.yaml
```

The full example is `config.example.yaml`. The most important keys:

| Key | Meaning |
|---|---|
| `document.path` | The document. It is never modified; with `open_copy: true` the reader opens a read-only copy in `<run>/work/`. Set `open_copy: false` if the wrapper or licence is bound to the original path |
| `document.known_page_count` | Makes end detection exact. Strongly recommended when known |
| `document.max_pages` | Hard cap. Hitting it is reported as "possibly incomplete" |
| `viewer.profile` | `acrobat`, `pdfxchange` or `generic`. Defaults come from `viewers/profiles.py`; anything you set overrides them |
| `viewer.document_pane` | Win32 child window that renders pages (Acrobat: `AVL_AVView` / `AVPageView`). Find yours with `calibrate` |
| `viewer.viewport_margins` | Fallback crop `[l, t, r, b]` of the client area when no pane matches |
| `viewer.keys.*` | Navigation keys in pywinauto syntax (`^`=Ctrl, `+`=Shift, `%`=Alt, `{RIGHT}`, `{PGDN}`). `alt_next_page` keys are tried on retries |
| `viewer.goto_method` | `dialog` (Acrobat `Ctrl+Shift+N`), `page_field`, `sequential`, or `manual`. Used when resuming |
| `viewer.page_indicator` | Regexes for reading "3 / 12" from UI Automation, plus an optional OCR region fallback |
| `capture.mode` | `fit_page` or `scroll` |
| `capture.stability.*` | Frame interval, consecutive stable frames, change thresholds, timeout |
| `capture.placeholder_text_re` | Wrapper "please wait" / "enable JavaScript" texts. A capture matching one is retried, then flagged |
| `prompts.rules` | Known-prompt handling: see [Manual checkpoints](#manual-checkpoints) |
| `manual.mode` | `messagebox`, `console`, or `abort` (unattended) |
| `safety.estop_hotkey` | Default `ctrl+shift+f12` |

### Prompt rules

```yaml
prompts:
  unknown_action: manual        # or abort; never "dismiss"
  rules:
    - name: untagged-document-notice
      text_re: '(?i)reading untagged document'
      action: click             # click | keys | wait | manual | abort | ignore
      button: Cancel
      max_times: 3
    - name: corporate-wrapper-consent      # security-looking: needs explicit acknowledgement
      title_re: '^Document Wrapper$'
      text_re: '(?i)allow this document to run'
      action: click
      button: Allow
      acknowledge_security: true           # your decision, per prompt, in config
```

Rules are evaluated in order: yours first, then the profile's. A `click`/`keys` rule on a dialog whose text matches `prompts.security_text_re` (JavaScript, trust, certificate, password, permission, allow/block, protected view, sign-in, licence) is **not** executed unless the rule sets `acknowledge_security: true`. Without it, the harness pauses for the operator. Each rule acts at most `max_times` times per run; after that the harness assumes a loop and asks the operator.

---

## CLI usage

All commands are run as `python -m doc_capture_harness <command>`. Viewer commands need Windows and an interactive desktop. `ocr` and `report` work anywhere.

Inspect the environment: list windows, monitors and scaling.

```powershell
python -m doc_capture_harness list-windows
```

Calibrate a reader. This writes annotated screenshots, the child-window list (pane candidates), the UIA tree, dialogs, the page indicator reading, and a next/prev navigation test to `.\calibration\`:

```powershell
python -m doc_capture_harness calibrate -c my-test.yaml --test-nav
```

A full run (capture, OCR, report):

```powershell
python -m doc_capture_harness run -c my-test.yaml
```

A run with overrides: a known page count, a named run directory, and console checkpoints:

```powershell
python -m doc_capture_harness run -c my-test.yaml --known-pages 42 --run-dir runs\contract-v3 --manual console
```

Attach to a document you already opened (useful when the wrapper needs a manual login first):

```powershell
python -m doc_capture_harness run -c my-test.yaml --attach
```

Tall pages: fit width plus overlapping scroll captures:

```powershell
python -m doc_capture_harness run -c my-test.yaml --mode scroll
```

Capture now and OCR later (OCR can run on another machine):

```powershell
python -m doc_capture_harness run -c my-test.yaml --no-ocr
```

```powershell
python -m doc_capture_harness ocr --run-dir runs\contract-v3 --lang eng+deu
```

Also write the searchable PDF next to the original (as a new `<name>.ocr.pdf`):

```powershell
python -m doc_capture_harness run -c my-test.yaml --pdf-next-to-original
```

Rebuild the PDF later, for example after `ocr --redo`, or to a chosen path:

```powershell
python -m doc_capture_harness pdf --run-dir runs\contract-v3 --next-to-original --overwrite
```

```powershell
python -m doc_capture_harness pdf --run-dir runs\contract-v3 --output D:\Review\contract-v3.ocr.pdf
```

Resume an interrupted, aborted or emergency-stopped run. This reuses the run's saved config and refuses if the document hash has changed (`--force` overrides):

```powershell
python -m doc_capture_harness resume --run-dir runs\contract-v3
```

Re-run OCR with different settings, and rebuild or print the report:

```powershell
python -m doc_capture_harness ocr --run-dir runs\contract-v3 --redo
```

```powershell
python -m doc_capture_harness report --run-dir runs\contract-v3 --print
```

**Emergency stop:** press **Ctrl+Shift+F12**, create a file named `STOP` in the run directory, press Ctrl+C in the console, or (while a pyautogui fallback action runs) move the mouse into a screen corner. The run stops before its next keystroke or capture, saves its state, and can be resumed.

---

## Outputs and exit codes

```
runs/<run>/
  pages/page_0001.png …      cropped page captures
  ocr/page_0001.txt …        per-page OCR text, in reading order
  ocr/page_0001.tsv …        word boxes + confidences
  ocr/combined.txt           all pages, "===== Page N =====" separators, missing pages marked
  output/<stem>.ocr.pdf      searchable PDF (page images + invisible OCR text layer)
  report.md, report.json     verification report
  manifest.json              resumable state (per-page status, hashes, flags, timings, events)
  config.resolved.json       effective configuration
  logs/harness.jsonl         structured log (one JSON object per event)
  diagnostics/               screenshot + JSON state for every failure/checkpoint
  slices/                    raw scroll slices (if output.save_raw_slices)
  work/                      read-only working copy of the document
```

The report contains:

- status, and end-of-document reason with an explanation and certainty
- expected, captured, failed and missing page counts
- total retries and pages with retries
- prompts handled, manual checkpoints, and pyautogui fallback uses
- OCR confidence summary: mean, lowest and p10 of the page means; low-confidence pages
- timings per phase and per page
- the original document's hash before and after the run
- environment: OS, DPI awareness, monitors and scale factors, reader executable
- the searchable PDF: files, page count, text layer, placeholder pages, verification
- a per-page table, and a **"Pages needing review"** list with flags

| Exit code | Meaning |
|---|---|
| 0 | Completed; every page clean; end of document certain |
| 2 | Completed, but some pages need review, the end was heuristic/limited, the original changed, or PDF verification failed |
| 3 | Stopped by the operator or emergency stop (resumable) |
| 4 | Error (see `logs/` and `diagnostics/`) |

Page flags that mark a page for review:

- `render_not_stable`
- `all_black_capture`
- `placeholder_text_detected`
- `stitch_overlap_uncertain`
- `max_scroll_slices_reached`
- `page_indicator_mismatch`
- `obstruction_detected`
- `low_ocr_confidence`
- `text_too_small_for_reliable_ocr`
- `page_crop_fallback_to_viewport`
- `capture_failed`
- `manual_override_used`
- `advance_after_retry_unverified`

Informational flags:

- `identical_to_previous_page`
- `duplicate_of_other_page`
- `blank_or_uniform`
- `advance_not_verified_by_page_indicator`
- `no_text_recognised`

---

## End-of-document detection

The first rule that applies wins:

1. **`known_page_count`.** Capture stops after that page. If navigation stalls earlier, that is a **navigation failure** and triggers a manual checkpoint, never "the end". If the reader reports a different total, the disagreement is logged and shown in the report.
2. **Reader-reported total.** When the page indicator reads "N / M", capture stops after page M. No key is pressed past the last page.
3. **`max_pages`.** A safety cap, reported as possibly incomplete.
4. **Heuristic stall.** "Next page" is retried with alternative keys. If neither the indicator nor the view changes:
   - With a readable indicator that is not at the total, this is a **failure**.
   - Without an indicator, an **end probe** runs. It goes back one page (the view must change), then forward again (the view must return to the same image). Only if both hold is the end reported as `navigation_stalled_end_confirmed_by_probe`. Otherwise it is a failure, or `..._not_confirmed` for one-page documents and unresponsive readers.

**Identical-looking pages.** Identical consecutive pages, such as blank separators or repeated forms, are accepted as distinct pages **only when the page indicator proves the reader advanced**. They get the flag `identical_to_previous_page`. Without an indicator, the harness cannot distinguish two such pages from the last page.

**Known limitations of end detection:**

- With no indicator and no known count, identical trailing pages are indistinguishable from the end. The probe confirms that navigation works, not that no further pages exist. The report marks such runs as `end_is_uncertain` (exit code 2).
- Without an indicator, a retried key press can overshoot when the first press advanced to an identical-looking page. Pages that advanced only on a retry are flagged `advance_after_retry_unverified`.
- Wrappers that load pages lazily may change the reported total during the run. The harness re-reads it on every page, and `known_page_count` still wins.
- A reader in two-page or continuous layout can make "next" move by two pages or scroll part of a page. Overshoot is corrected once when the indicator shows it; otherwise use Single Page layout.

**Recommendation:** supply `known_page_count` when you know it, and make sure `calibrate` shows a readable indicator.

---

## Reliability mechanisms

| Concern | Mechanism |
|---|---|
| Window identification | Matches process image name, window class regex and title regex (with the document name), excluding owned windows. A single-instance reader hand-off is handled by searching all windows, not the launched PID. If several windows match, a checkpoint asks you to bring the right one to the front |
| Document area | A Win32 child-window selector for the render pane, falling back to the client area minus configured margins, then cropped to the page by canvas colour |
| Loading states | Frame-difference stability (N identical frames), the reader's busy signals (wait cursor, `IsHungAppWindow`, title regex), placeholder-text OCR, and a startup timeout that leads to a checkpoint. A small `min_wait_s` only covers the gap before the first repaint |
| Dialogs and obstructions | Every viewer-process top-level dialog, and any other app's window above the reader that intersects the document area, is checked before and after each capture. A dialog that appears during a capture invalidates that capture |
| Focus | The harness verifies the reader is in the foreground before each key press and refocuses it (restoring a minimised window). If that fails, it pauses at a checkpoint instead of typing into the wrong window. Acrobat also focuses the page pane, so arrow keys do not go to a toolbar box |
| Hover artefacts | The mouse pointer is parked on the reader's title bar before captures |
| Display scaling | The process is made per-monitor-DPI-aware (v2) before any GUI import, so window rectangles and captures are both in physical pixels. Monitor scale factors are recorded |
| Multiple monitors | `mss` handles negative coordinates and secondary monitors. A document area spanning monitors with different DPI, or partly off-screen, triggers a checkpoint |
| Zoom | `prepare_keys`, plus a word-height check: small text is re-OCRed at 2× and flagged if still small |
| Retries | Bounded retries for capture and navigation, with alternative keys on retries. Every retry is counted and its reason recorded |
| Duplicates | A dHash comparison against all earlier pages flags `duplicate_of_other_page` (informational) |
| Original document | SHA-256 before and after the run. Opened as a read-only copy by default. Resume refuses a changed document |
| Resume | `manifest.json` is rewritten atomically after each page. `resume` continues at the first uncaptured page, navigates there (go-to dialog, page field, sequential, or manual), and verifies the position with the indicator. Without a readable indicator it asks you to confirm, except for sequential stepping, which is trusted by count |
| Input | pywinauto `SendInput` first, pyautogui only as a fallback (counted in the report). Each input checks the emergency stop |
| Diagnostics | On every failure or checkpoint: a screenshot of the reader window (or the whole desktop if `diagnostics_full_screen`), plus JSON with the rectangles, dialog list and indicator |

## Manual checkpoints

A checkpoint saves diagnostics, then asks the operator. In `messagebox` mode the choices are **Abort**, **Retry** (re-check now) and **Ignore** (proceed as if resolved). Console mode adds **skip**. In `abort` mode every checkpoint aborts the run, which stays resumable; use it for unattended runs.

Checkpoints fire on:

| Checkpoint | When |
|---|---|
| `setup` | After the document opens (profile-defined: page layout, panels) |
| `dialog` | Unknown dialog, security-looking dialog without acknowledgement, a `manual` rule, or a rule over `max_times` |
| `obstruction` | Another application's window covers the document area |
| `startup` | Document not loaded or stable within `startup_timeout_s` |
| `navigation_failed` | Could not advance. Navigate yourself, then Retry to verify or Ignore to accept |
| `focus_lost` | The reader could not be brought to the front |
| `ambiguous_window` / `window_not_found` | Window identification problems |
| `goto_start` / `verify_page` | Resume position could not be verified |
| `offscreen` / `mixed_dpi` | Window placement problems |

---

## Supporting another reader

1. Run `list-windows`, then `calibrate -c cfg.yaml --viewer generic --test-nav`. Note the following from the output:
   - `children.txt`: the class and title of the largest child window that shows the page. Use it for `document_pane`.
   - `uia_tree.txt`: names of the page-number controls. Use them for `page_indicator.control_name_re`.
   - `navigation.json`: whether the keys work.
2. Put those values under `viewer:` with `profile: generic`.
3. If behaviour differs in code (special focus handling, a custom go-to), subclass `Win32ViewerAdapter`, add a profile in `viewers/profiles.py`, and register it in `viewers/__init__.py`. The runner, OCR and report need no change.

---

## Assumptions and limitations

- **Interactive desktop required.** UI automation and screen capture need an unlocked, visible session. RDP sessions must stay connected and not minimised. Windows services and Session 0 cannot do this. `PrintWindow` capture (`capture.method: printwindow`) works while the window is covered, but returns black for many GPU-accelerated views; this is detected, not worked around.
- **Reader and version dependence.** Window classes, the Acrobat `AVL_AVView`/`AVPageView` pane, UIA names of the page indicator, and keyboard shortcuts vary by reader version, language, edition and enterprise policy. The profiles are starting points: verify them with `calibrate`. The PDF-XChange profile ships without a pane selector and uses sequential go-to for that reason.
- **Wrapper dependence.** The harness cannot know what a wrapper considers "loaded". It relies on visual stability, busy signals and your `placeholder_text_re`. A wrapper that animates continuously never becomes stable and is flagged `render_not_stable` after retries; you may need a higher `changed_fraction_threshold`. A wrapper that shows a watermark or overlay will have it captured and OCRed.
- **Page indicator reading.** UIA text extraction is heuristic. Implausible values (current > total) are discarded. If an operator override contradicts the indicator, the indicator is distrusted for the rest of the run.
- **Prompt detection** covers Win32 top-level windows. In-page HTML-style prompts drawn inside the document area are not "dialogs". Add their text to `placeholder_text_re` so the capture is retried, flagged and left to you.
- **Cropping** assumes a uniform canvas colour around the page. A page that fills the viewport, or a white canvas, keeps the whole viewport and is flagged `page_crop_fallback_to_viewport`.
- **Scroll stitching** needs textured overlap. Long blank stretches make the overlap ambiguous (`stitch_overlap_uncertain`). Without a page indicator, scrolling past a page boundary in Single Page layout cannot be detected. Prefer `fit_page` mode, or ensure the indicator is readable.
- **OCR quality** depends on on-screen resolution: zoom, monitor DPI and font rendering. Confidence values are Tesseract's word-level scores, not accuracy measurements. Compare against ground truth where accuracy matters.
- **Security posture.** The harness never enables document JavaScript globally, never auto-dismisses unknown dialogs, and acts on security-looking dialogs only where your config explicitly says so. It does not circumvent capture exclusion or other protection. Whether automated capture is permitted for a given document is your responsibility.
- **Not validated on real readers in this repository's CI.** The logic is tested end-to-end against a simulated reader (`tests/fakes.py`). The Windows adapter code needs to be validated on your reader version with `calibrate` before production use.

## Testing

```bash
python -m pytest -q
```

The tests cover:

- config validation and profile merging
- stability detection, page cropping, overlap stitching and duplicate hashing
- the prompt policy, including security acknowledgement, unknown dialogs, obstructions and `max_times`
- end-to-end simulated runs: indicator-driven ends, identical pages, the no-indicator end probe, known-page-count checkpoints, flaky navigation, the `max_pages` limit, dialogs, emergency stop with resume, refusal to resume a changed document, scroll-mode stitching, unattended abort, and real OCR when Tesseract is installed
- searchable PDF output: text layer extraction, page size and metadata from the original, encrypted originals left encrypted, refusal to overwrite the original, writing next to the original with overwrite protection, placeholder pages, and the image-only fallback
