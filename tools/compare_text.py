"""Compare a run's OCR text with ground truth, page by page.

Ground truth is either a normal (text-based) PDF of the same document, or a
folder of page_NNNN.txt files. Prints per page and overall:
  recall   share of reference words found in the OCR text, ignoring order
           (robust to column/reading-order differences); used for pass/fail
  ordered  share matched in sequence (difflib); low values with high recall
           mean reading-order differences rather than recognition errors

    python tools/compare_text.py --run-dir runs/test1 --reference reference.pdf
    python tools/compare_text.py --run-dir runs/test1 --reference truth_txt/ --min-accuracy 0.95
"""
import argparse
import collections
import difflib
import re
import sys
from pathlib import Path


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def reference_pages(ref: Path) -> list[str]:
    if ref.is_dir():
        return [p.read_text(encoding="utf-8") for p in sorted(ref.glob("page_*.txt"))]
    from pypdf import PdfReader
    return [p.extract_text() or "" for p in PdfReader(str(ref)).pages]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--reference", required=True, help="text-based PDF or folder of page_NNNN.txt")
    ap.add_argument("--min-accuracy", type=float, default=0.90)
    a = ap.parse_args()
    ref = reference_pages(Path(a.reference))
    ocr_dir = Path(a.run_dir) / "ocr"
    n = max(len(ref), len(list(ocr_dir.glob("page_*.txt"))))
    total_ref = total_match = 0
    bad = []
    print(f"{'page':>4}  {'ref words':>9}  {'ocr words':>9}  {'recall':>7}  {'ordered':>7}")
    for i in range(1, n + 1):
        r = words(ref[i - 1]) if i <= len(ref) else []
        f = ocr_dir / f"page_{i:04d}.txt"
        o = words(f.read_text(encoding="utf-8")) if f.exists() else []
        match = sum((collections.Counter(r) & collections.Counter(o)).values())
        seq = sum(b.size for b in difflib.SequenceMatcher(None, r, o, autojunk=False).get_matching_blocks())
        acc = match / len(r) if r else (1.0 if not o else 0.0)
        ordered = seq / len(r) if r else acc
        total_ref += len(r)
        total_match += match
        mark = "" if acc >= a.min_accuracy else "  <-- below threshold" + ("" if f.exists() else " (no OCR file)")
        if mark:
            bad.append(i)
        print(f"{i:>4}  {len(r):>9}  {len(o):>9}  {acc:>7.1%}  {ordered:>7.1%}{mark}")
    overall = total_match / total_ref if total_ref else 0.0
    print(f"\noverall word recall: {overall:.1%}  pages below {a.min_accuracy:.0%}: {bad or 'none'}")
    if len(ref) != len(list(ocr_dir.glob('page_*.txt'))):
        print(f"page count differs: reference {len(ref)}, OCR {len(list(ocr_dir.glob('page_*.txt')))}")
    return 0 if not bad and overall >= a.min_accuracy else 1


if __name__ == "__main__":
    sys.exit(main())
