"""Searchable-PDF assembly from captured pages."""
import json

import pytest
from pypdf import PdfReader, PdfWriter

from conftest import fast_config, needs_tesseract
from doc_capture_harness.errors import HarnessError
from doc_capture_harness.pdf_output import build_pdf
from doc_capture_harness.state import Manifest, RunPaths, sha256_file
from fakes import Scenario, make_page
from test_simulated_runs import run


def _real_pdf(path, n, size=(595.276, 841.89), title="Quarterly Statement", encrypt=False):
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(*size)
    w.add_metadata({"/Title": title, "/Author": "Test Owner"})
    if encrypt:
        w.encrypt(user_password="u", owner_password="o")
    with open(path, "wb") as f:
        w.write(f)
    return path


def _pdf_run(doc, tmp_path, pages=3, ocr=True, **pdf):
    cfg = fast_config(doc, pdf={"enabled": True, **pdf})
    sc = Scenario([make_page(i) for i in range(1, pages + 1)])
    code, h, rep, v = run(cfg, sc, tmp_path / "r", skip_ocr=not ocr)
    return cfg, code, h, rep


@needs_tesseract
def test_searchable_pdf_from_run(doc, tmp_path):
    before = sha256_file(doc)
    cfg, code, h, rep = _pdf_run(doc, tmp_path)
    out = tmp_path / "r" / "output" / f"{doc.stem}.ocr.pdf"
    assert code == 0 and rep["pdf"]["files"] == [str(out)]
    r = PdfReader(str(out))
    assert len(r.pages) == 3
    for i in range(3):
        assert f"Page {i + 1}" in r.pages[i].extract_text()
    assert r.metadata["/CaptureSourceSHA256"] == before
    assert rep["pdf"]["verification"]["ok"] and rep["pdf"]["verification"]["pages_with_text"] == 3
    assert sha256_file(doc) == before                      # original untouched
    assert "Searchable PDF" in (tmp_path / "r" / "report.md").read_text()


@needs_tesseract
def test_page_size_and_metadata_from_original(tmp_path):
    doc = _real_pdf(tmp_path / "statement.pdf", 1)          # wrapper-style: 1 placeholder page
    cfg, code, h, rep = _pdf_run(doc, tmp_path, pages=3)
    r = PdfReader(rep["pdf"]["files"][0])
    assert len(r.pages) == 3
    assert all(abs(float(p.mediabox.width) - 595.276) < 0.5 for p in r.pages)   # A4 width from original
    assert r.metadata["/Title"] == "Quarterly Statement" and r.metadata["/Author"] == "Test Owner"
    assert "original file has 1 page" in rep["pdf"]["page_count_note"]


@needs_tesseract
def test_encrypted_original_is_not_decrypted(tmp_path):
    doc = _real_pdf(tmp_path / "locked.pdf", 2, encrypt=True)
    cfg, code, h, rep = _pdf_run(doc, tmp_path, pages=2)
    pdf = rep["pdf"]
    assert pdf["original_pdf"]["encrypted"] and not pdf["original_pdf"]["readable"]
    r = PdfReader(pdf["files"][0])
    assert not r.is_encrypted
    assert all(abs(float(p.mediabox.width) - 612) < 0.5 for p in r.pages)       # letter fallback
    assert "/Author" not in r.metadata


def _manifest(tmp_path):
    paths = RunPaths(tmp_path / "r")
    return paths, Manifest.load(paths)


def test_refuses_to_write_over_original(doc, tmp_path):
    cfg, code, h, rep = _pdf_run(doc, tmp_path, pages=1, ocr=False, text_layer=False)
    paths, m = _manifest(tmp_path)
    with pytest.raises(HarnessError, match="original"):
        build_pdf(cfg, m, paths, explicit_output=doc)
    cfg.pdf.output_name = doc.name
    with pytest.raises(HarnessError, match="original"):
        build_pdf(cfg, m, paths, next_to_original=True)


def test_next_to_original_and_overwrite(doc, tmp_path):
    cfg, code, h, rep = _pdf_run(doc, tmp_path, pages=2, ocr=False, text_layer=False)
    paths, m = _manifest(tmp_path)
    res = build_pdf(cfg, m, paths, next_to_original=True)
    beside = doc.parent / f"{doc.stem}.ocr.pdf"
    assert str(beside) in res["files"] and beside.exists()
    with pytest.raises(HarnessError, match="already exists"):
        build_pdf(cfg, m, paths, next_to_original=True)
    build_pdf(cfg, m, paths, next_to_original=True, overwrite=True)
    build_pdf(cfg, m, paths)                                 # run-dir output is always replaceable


def test_missing_page_gets_placeholder(doc, tmp_path):
    cfg, code, h, rep = _pdf_run(doc, tmp_path, pages=3, ocr=False, text_layer=False)
    paths, m = _manifest(tmp_path)
    rec = m.page(2)
    rec["status"] = "failed"
    m.set_page(2, rec)
    res = build_pdf(cfg, m, paths)
    assert res["pages"] == 3 and res["placeholder_pages"] == [2]
    cfg.pdf.missing_pages = "skip"
    res = build_pdf(cfg, m, paths)
    assert res["pages"] == 2 and res["skipped_pages"] == [2]


def test_image_only_fallback_without_tesseract(doc, tmp_path, monkeypatch):
    import doc_capture_harness.ocr as ocr

    def missing(_cfg):
        raise FileNotFoundError("tesseract executable not found")

    cfg, code, h, rep = _pdf_run(doc, tmp_path, pages=2, ocr=False, enabled=False)
    monkeypatch.setattr(ocr, "resolve_tesseract", missing)
    paths, m = _manifest(tmp_path)
    cfg.pdf.enabled = True
    res = build_pdf(cfg, m, paths)
    assert not res["text_layer"] and "image-only" in res["note"]
    assert res["verification"]["ok"] and len(PdfReader(res["files"][0]).pages) == 2
    assert json.loads((paths.manifest).read_text())["pdf"]["text_layer"] is False
