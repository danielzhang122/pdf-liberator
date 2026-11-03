"""Build the downloadable release zip: python tools/make_release.py [version]"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["Capture.bat", "install.ps1", "START-HERE.txt", "README.md", "requirements.txt",
         "config.example.yaml", "config.yaml", "tools/compare_text.py"]
CRLF = {".bat", ".ps1", ".txt", ".yaml"}


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from doc_capture_harness import __version__
    version = sys.argv[1].lstrip("v") if len(sys.argv) > 1 else __version__
    name = f"doc-capture-harness-v{version}"
    out = ROOT / "dist" / f"{name}.zip"
    out.parent.mkdir(exist_ok=True)
    paths = [ROOT / f for f in FILES] + sorted((ROOT / "doc_capture_harness").rglob("*.py"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            data = p.read_bytes()
            if p.suffix.lower() in CRLF:
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            z.writestr(f"{name}/{p.relative_to(ROOT).as_posix()}", data)
    print(out)


if __name__ == "__main__":
    main()
