# doc_capture_harness

A Windows test harness that opens a document in a **desktop PDF reader** (Adobe Acrobat / Acrobat Reader, PDF-XChange, or a generic configured reader), captures every rendered page as PNG, runs **Tesseract OCR** locally, and writes a **verification report**.

Its a brute-force approach to making a protected PDF accessible to multiple users.

It also assembles the captures into a **searchable PDF**: each page is the captured image with an invisible OCR text layer, sized like the original's pages. The original file is never modified.

It is meant for documents whose real content only appears after the reader runs a viewer-specific wrapper or document JavaScript, so a PDF library sees only a placeholder page.

> **Scope.** Use it only on documents you own or are authorised to test. The harness captures what the reader shows on screen and nothing more. It does **not** decrypt files, bypass DRM, enable JavaScript globally, or dismiss security prompts. If a reader window asks Windows to exclude it from capture (display affinity), the run stops with `CaptureBlocked`.

---

## Quick start

**1. Set up (once per machine).** Right-click `install.ps1` and choose **Run with PowerShell**. It installs Python packages and Tesseract (through winget if missing), then runs `doctor` to check the machine. Fix anything marked `FAIL`.

**2. Set the reader's defaults (once).** In Acrobat: *Edit > Preferences > Page Display*, set **Page Layout = Single Page** and **Zoom = Fit Page**. (PDF-XChange: *Edit > Preferences > Page Display*, Default Page Layout = Single Page, Default Zoom = Fit Page.) Close side panels. Acrobat and PDF-XChange remember these settings for every document.

**3. Capture.** Drag a PDF, several PDFs, or a whole folder onto **`Capture.bat`**. For each document you get:

```
<input folder>\ocr_output\<name>.ocr.pdf                 searchable PDF (page images + OCR text layer)
<input folder>\ocr_output_details\<name>.ocr.txt         OCR text, "===== Page N =====" per page
<input folder>\ocr_output_details\batch_summary.md       one line per document: status, pages, pages to review
```

The batch runs **unattended**:

- Anything that needs a person, such as an unknown dialog, a wrong page layout, or failed navigation, sets that document aside as `needs_attention` and the batch moves on.
- Drag the same folder onto `Capture.bat` again to retry. Finished documents are skipped, interrupted ones resume where they stopped, and the set-aside ones are retried.
- Stop at any time with **Ctrl+Shift+F12**.

The same from a terminal:

```powershell
.venv\Scripts\python.exe -m doc_capture_harness batch C:\Docs
```

No config file is needed: the installed reader is detected automatically. See [Batch processing](#batch-processing) and [Large documents](#large-documents) for options.

---

## Contents

1. [Batch processing](#batch-processing)
2. [Large documents](#large-documents)
3. [How it works](#how-it-works)
4. [Installation](#installation)
5. [Tesseract setup](#tesseract-setup)
6. [Preparing the reader and desktop](#preparing-the-reader-and-desktop)
7. [Configuration](#configuration)
8. [CLI usage](#cli-usage)
9. [Outputs and exit codes](#outputs-and-exit-codes)
10. [End-of-document detection](#end-of-document-detection)
11. [Reliability mechanisms](#reliability-mechanisms)
12. [Manual checkpoints](#manual-checkpoints)
13. [Supporting another reader](#supporting-another-reader)
14. [Assumptions and limitations](#assumptions-and-limitations)
15. [Testing](#testing)

---

## Batch processing

```powershell
python -m doc_capture_harness batch C:\Docs                 # every PDF under C:\Docs (sub-folders too)
```

```powershell
python -m doc_capture_harness batch C:\Docs --out D:\OCR    # choose the output folder
```

```powershell
python -m doc_capture_harness batch list.csv --out D:\OCR    # an explicit list
```

```powershell
python -m doc_capture_harness batch C:\Docs --dry-run       # show what would be processed
```

- **Inputs:** folders (recursive by default; `--no-recursive` turns that off), individual files, a `.txt` list (one path per line), or a `.csv` with a `path` column and optional `known_pages` and `lang` columns. A known page count per document makes end-of-document detection exact. Existing `*.ocr.pdf` outputs are never picked up as inputs.
- **Outputs** go to two folders. The output folder (default `<input>\ocr_output`) holds only the searchable PDFs (`<name>.ocr.pdf`), mirroring the input sub-folders. Everything else goes to a sibling folder `<out>_details` (default `<input>\ocr_output_details`): `<name>.ocr.txt`, `batch_summary.csv`, `batch_summary.md`, and `_runs\`, which holds the full run folders (pages, report, logs, diagnostics) and `batch.json`. Name clashes get a short unique suffix.
- **Statuses:**
  - `completed`: clean.
  - `completed_with_warnings`: some pages are flagged for review, or the end of the document was heuristic. The per-document `report.md` says which.
  - `needs_attention`: stopped for something a person must resolve; the reason is in the summary.
  - `failed`: an error.
  - `stopped`: emergency stop.
- **Re-running is safe and incremental.** A document is skipped only if its SHA-256 is unchanged and its output exists. A changed document starts over; the old run is kept, renamed.
- **Unattended vs interactive:**
  - Default is unattended: no pop-ups after the start. The reader setup reminder is shown once at the start; `--no-prompt` skips it for scheduled runs.
  - `--interactive` pauses on problems with a message box instead, which suits a first run on a new kind of document.
- **Stopping:** press Ctrl+Shift+F12, or create a file named `STOP` in `<out>_details\_runs\`. Either stops the whole batch after the current step.
- **Time limit:** `--time-limit 240` stops a document whose capture runs over 240 minutes (resumable), so one stuck document cannot hold up the batch.
- **Options for every document:** `--config my.yaml` applies a configuration to every document. Also available: `--lang eng+deu`, `--workers N`, `--max-pages N`, `--no-pdf`.

## Watching progress and using the PC meanwhile

- **Progress page:** in a second window run `python -m doc_capture_harness dashboard <run or output folder>`. A browser page shows captured / expected pages, OCR progress and pages needing review for every run under that folder, refreshing every 2 s (localhost only, read-only).
- **Background mode:** add `--background` (or `capture.background: true`) to capture the reader window with PrintWindow and send navigation keys straight to it, so the harness never takes focus or moves the mouse. Keep the reader window open (not minimized). Limits: modifier shortcuts (e.g. Ctrl+0) may be ignored, page-box navigation is off (use `goto_method: dialog`), PDF-XChange may ignore posted keys, and GPU-rendered readers can produce black frames. Not yet verified on Acrobat or PDF-XChange.

## Large documents

Capture is sequential: one reader on one screen. With real Acrobat Reader, the CI benchmark ([Testing](#testing)) measured about **1.9–2.5 s per page** in steady state (20-page document, 1024×768 Windows runner). Everything else is built so that cost per page stays flat for documents with thousands of pages:

- **Parallel OCR while capturing.** Each page goes to a pool of Tesseract processes as soon as it is captured (default: CPU count − 1, at most 8; `--workers`). A single Tesseract pass per page produces the text, word data and the page's searchable PDF.
- **Streaming PDF assembly.** Page PDFs are merged with qpdf (pikepdf) in chunks of 200, so memory does not grow with the page count. Per-page intermediates are deleted afterwards unless `pdf.keep_page_pdfs: true`.
- **Constant-cost state.** Every page is saved as its own small record (`records/page_NNNN.json`). The run summary is rewritten at most every 5 s, not after every page. A crash loses no captured page, and `resume` continues from the next page.
- **Verification sampling.** The PDF text-layer check covers 12 evenly spaced pages (`pdf.verify_sample_pages`), plus an exact page count.

**Per-page console line.** Every captured page prints where its time went, for example `page 12/240 captured in 2.8s [render wait 1.6, dialog checks 0.1, page number 0.0, keys/other 1.1]`. Searches of the reader's interface (page number box, dialog contents) run once and are cached, never on every page. The page-number search skips the document content, and a failed search is retried at most once a minute. OCR processes run at below-normal priority, so they never slow the reader down.

Measured on a 4-core Linux machine with a simulated reader, 1100×1430-pixel pages, 30 lines of text each:

| Pages | OCR workers | Capture | OCR left after capture | PDF assembly | Total | Searchable PDF |
|---|---|---|---|---|---|---|
| 120 | 1 | 74 s | 121 s | 7 s | 204 s | 35 MB |
| 120 | 3 | 74 s | 1 s | 7 s | 83 s | 35 MB |
| 300 | 3 | 249 s | 1 s | 7 s | 259 s | 88 MB |

With 3 workers, OCR fully overlaps capture. These are simulated-reader figures; with a real reader, capture time depends on the reader's rendering speed (see the Acrobat Reader figure above).

Sizing guide. These are rough estimates; time a 50-page document on your reader and machine to get real numbers.

- **Disk:** plan for about 1–2 MB per page in the run folder (PNG captures; per-page PDFs are deleted after merging) and 0.3–1 MB per page for the searchable PDF, depending on the page content. `pdf.image_format: jpeg` makes the PDF several times smaller. `doctor --out-dir D:\OCR` checks free space.
- **Time:** capture is bounded by the stability check (`min_wait_s` + 3 frames: polled every `fast_interval_s` while the page is drawing, then every `interval_s` once frames agree, about 0.9 s with the defaults), done once per page, plus the reader's own rendering time. OCR uses one process per CPU core at below-normal priority, so little work remains after the last page.
- **Limits:** `document.max_pages` defaults to 5000 per document. Raise it in a config file, or use `--max-pages`, for longer documents.

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
6. It advances to the next page (see [How pages are turned](#how-pages-are-turned-acrobat)), verifies the move, and decides whether the document has ended (see [End-of-document detection](#end-of-document-detection)). After every page turn the view is **settled** first: any dialog or other window that appeared is handled or waited out, and the focus is taken back if another window took it. Only then is the view compared with the previous page, so a pop-up appearing or closing is never mistaken for a page turn.

OCR runs after capture, then the searchable PDF is built. Both can also be run later with the `ocr` and `pdf` commands, on any OS.

### How pages are turned (Acrobat)

`viewer.navigate_by` chooses how the harness moves to the next page:

| Value | How | Default for |
|---|---|---|
| `goto` | Go to page N+1 by number, through the reader's Go To Page dialog | `acrobat` profile |
| `keys` | Press `keys.next_page` (for example `{RIGHT}`) | `pdfxchange`, `generic` |

**Why not just press a key?** Keyboard focus can be stolen by Acrobat pop-ups, such as the "pin to taskbar" window, so a key press can go to the wrong window. A key press that is delivered late can also arrive together with the retry and move two pages. Going to a page by number is immune to both: a lost or repeated request still lands on the same page.

**How `goto` works.** The harness presses **Ctrl+Shift+N** to open Acrobat's *Go To Page* dialog, writes the page number straight into the dialog's page box, and presses **OK** directly (a window message to the button, not a keystroke). No further keys are typed. A Go To Page dialog left open is cancelled by the built-in rule `acrobat-stray-go-to-page-dialog`.

**Fallback.** `goto` is used only when the page count is known (`known_page_count`, or the count read from the Go To Page dialog, see [End-of-document detection](#end-of-document-detection)). Without a page count, or if the dialog cannot be used, the harness falls back to key presses.

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
| Use **Single Page** layout, not continuous | Each "next page" should show exactly one page, starting at its top | Set once in the reader's *Preferences > Page Display*, which then applies to every document. Checked automatically: if more than one page is visible, or the page is cut off, the document pauses (interactive) or is set aside as `needs_attention` (batch) |
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
| `viewer.navigate_by` | `goto` (Acrobat default: next page by number through the Go To Page dialog) or `keys` (press `keys.next_page`). See [How pages are turned](#how-pages-are-turned-acrobat) |
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

**Windows from other programs** (for example a notification pop-up) that cover the page are first given `prompts.obstruction_wait_s` (default 30 s) to close on their own. If one stays, the document pauses or is set aside. To handle a specific program's window automatically, add a rule with `scope: other`, for example:

```yaml
    - name: pdfmonitor-notification
      scope: other              # viewer (default) | other | any
      class_re: 'PDFMonitor\.exe'
      title_re: '^NotifyView$'
      action: wait              # or click (with button:), or ignore (the window is then captured too)
      wait_s: 5
      max_times: 24
```

**`config.yaml` next to `Capture.bat` is loaded automatically**, so drag-and-drop runs use your settings. A commented starter file is included in the release.

Rules are evaluated in order: yours first, then the profile's. A `click`/`keys` rule on a dialog whose text matches `prompts.security_text_re` (JavaScript, trust, certificate, password, permission, allow/block, protected view, sign-in, licence) is **not** executed unless the rule sets `acknowledge_security: true`. Without it, the harness pauses for the operator. Each rule acts at most `max_times` times per run; after that the harness assumes a loop and asks the operator.

---

## CLI usage

All commands are run as `python -m doc_capture_harness <command>`. Viewer commands need Windows and an interactive desktop. `ocr`, `pdf`, `report` and `batch --dry-run` work anywhere.

Check that this machine is ready:

```powershell
python -m doc_capture_harness doctor
```

One document, no config file needed:

```powershell
python -m doc_capture_harness run C:\Docs\statement.pdf --known-pages 42
```

Many documents: see [Batch processing](#batch-processing).

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
2. **The reader's own page count**, read once from the Go To Page dialog before capture starts (Acrobat: "Page: [ ] of N"; the dialog is then cancelled). Capture stops exactly after page N, and nothing is pressed past it. As with `known_page_count`, a stall before page N is a **navigation failure**, never "the end". It is not read when `known_page_count` is set.
3. **Reader-reported total.** When the page indicator reads "N / M", capture stops after page M. No key is pressed past the last page. If both this and the dialog count are available, the indicator's live total is used.
4. **`max_pages`.** A safety cap, reported as possibly incomplete.

The remaining rule is a **fallback** for when no page count is available:

5. **Heuristic stall.** "Next page" is retried with alternative keys. If neither the indicator nor the view changes:
   - With a readable indicator that is not at the total, this is a **failure**.
   - Without an indicator, an **end probe** runs. It goes back one page (the view must change), then forward again (the view must return to the same image). Only if both hold is the end reported as `navigation_stalled_end_confirmed_by_probe`. Otherwise it is a failure, or `..._not_confirmed` for one-page documents.

   Without any page count, **unresponsive navigation is never taken as the end**: if "previous" has no effect either, it is a navigation failure, and in a batch the document is set aside as `needs_attention`.

**Identical-looking pages.** Identical consecutive pages, such as blank separators or repeated forms, are accepted as distinct pages **only when the page indicator proves the reader advanced**. They get the flag `identical_to_previous_page`. Without an indicator, the harness cannot distinguish two such pages from the last page.

**Known limitations of end detection:**

- With no indicator and no known count, identical trailing pages are indistinguishable from the end. The probe confirms that navigation works, not that no further pages exist. The report marks such runs as `end_is_uncertain` (exit code 2).
- Without an indicator, a retried key press can overshoot when the first press advanced to an identical-looking page. Pages that advanced only on a retry are flagged `advance_after_retry_unverified`.
- Wrappers that load pages lazily may change the reported total during the run. The page indicator is re-read on every page, but the Go To Page dialog count is read only once, before capture. `known_page_count` still wins.
- A reader in two-page or continuous layout can make "next" move by two pages or scroll part of a page. Overshoot is corrected once when the indicator shows it; otherwise use Single Page layout.

**Recommendation:** supply `known_page_count` when you know it. Otherwise check in the log (`page_count` event) that the reader's page count was read, or make sure `calibrate` shows a readable indicator.

---

## Reliability mechanisms

| Concern | Mechanism |
|---|---|
| Window identification | Matches process image name, window class regex and title regex (with the document name), excluding owned windows. A single-instance reader hand-off is handled by searching all windows, not the launched PID. If several windows match, a checkpoint asks you to bring the right one to the front |
| Document area | A Win32 child-window selector for the render pane, falling back to the client area minus configured margins, then cropped to the page by canvas colour |
| Loading states | Frame-difference stability (N identical frames), the reader's busy signals (wait cursor, `IsHungAppWindow`, title regex), placeholder-text OCR, and a startup timeout that leads to a checkpoint. A small `min_wait_s` only covers the gap before the first repaint |
| Dialogs and obstructions | Every viewer-process top-level dialog, and any other app's window above the reader that intersects the document area, is checked before and after each capture. A dialog that appears during a capture invalidates that capture |
| Pop-ups during page turns | After every page turn the view is settled before it is judged: dialogs and other windows are handled or waited out and the focus is regained, then the view is observed again. A pop-up is never counted as a page turn |
| Focus | The harness verifies the reader is in the foreground before each key press and refocuses it (restoring a minimised window). If that fails, it pauses at a checkpoint instead of typing into the wrong window. Acrobat also focuses the page pane, so arrow keys do not go to a toolbar box |
| Hover artefacts | The mouse pointer is parked on the reader's title bar before captures |
| Display scaling | The process is made per-monitor-DPI-aware (v2) before any GUI import, so window rectangles and captures are both in physical pixels. Monitor scale factors are recorded |
| Multiple monitors | `mss` handles negative coordinates and secondary monitors. A document area spanning monitors with different DPI, or partly off-screen, triggers a checkpoint |
| Zoom | `prepare_keys`, plus a word-height check: small text is re-OCRed at 2× and flagged if still small |
| Retries | Bounded retries for capture and navigation, with alternative keys on retries. Every retry is counted and its reason recorded |
| Duplicates | A dHash comparison against all earlier pages flags `duplicate_of_other_page` (informational) |
| Original document | SHA-256 before and after the run. Opened as a read-only copy by default. Resume refuses a changed document |
| Resume | `manifest.json` is rewritten atomically after each page. `resume` returns to the last captured page (go-to dialog, page field, sequential, or manual), verifies the position with the indicator or, without one, by comparing the view with that page's saved image, and then continues with the next page like a normal run. Without a readable indicator it asks you to confirm, except for sequential stepping, which is trusted by count |
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
- **Limited real-reader validation in this repository's CI.** The logic is tested end-to-end against a simulated reader (`tests/fakes.py`); the only real reader exercised is Acrobat Reader, in the on-demand benchmark (see [Testing](#testing)). The Windows adapter code needs to be validated on your reader version with `calibrate` before production use.

## Testing

```bash
python -m pytest -q
```

The tests cover:

- config validation and profile merging
- stability detection, page cropping, overlap stitching and duplicate hashing
- the prompt policy, including security acknowledgement, unknown dialogs, obstructions and `max_times`
- end-to-end simulated runs: indicator-driven ends, identical pages, the no-indicator end probe, known-page-count checkpoints, flaky navigation, the `max_pages` limit, dialogs, emergency stop with resume, refusal to resume a changed document, scroll-mode stitching, unattended abort, and real OCR when Tesseract is installed
- scale and automation: batch processing (skip finished documents, retry ones set aside, mirrored outputs), the batch-wide STOP file, CSV inputs, a 600-page document with flat per-page cost, page records surviving a stale header, chunked PDF merging, page PDFs reused from the OCR pass, layout detection (continuous and zoomed), and the per-document time limit
- searchable PDF output: text layer extraction, page size and metadata from the original, encrypted originals left encrypted, refusal to overwrite the original, writing next to the original with overwrite protection, placeholder pages, and the image-only fallback

### Real Acrobat Reader benchmark

`tools/acrobat_benchmark.py` measures capture speed with a real reader. It generates a text PDF (20 pages by default, opening in Single Page + Fit Page), captures it with the harness, and reports seconds per page taken from the harness's own log. It exits with code 1 if the average exceeds `--limit` (default 10 s) or the document was not captured completely.

```powershell
.venv\Scripts\python.exe tools\acrobat_benchmark.py C:\Temp\bench --pages 20
```

The `acrobat-benchmark` GitHub workflow (`.github/workflows/acrobat-benchmark.yml`, started manually) runs it on a Windows runner: it installs the harness with `install.ps1`, installs Adobe Acrobat Reader, pre-accepts its first-run prompts, captures the generated 20-page PDF, and uploads the benchmark results, logs and reports.
