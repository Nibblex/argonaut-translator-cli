import pymupdf as fitz
import pytest

from argonaut.pdf import FastPdfTranslator, count_pdf_paragraphs, is_horizontal
from argonaut.translation import CancelledError
from tests.conftest import FakeTranslation


def make_pdf(path, paragraphs=("Hello world", "Second paragraph")):
    doc = fitz.open()
    page = doc.new_page()
    for i, text in enumerate(paragraphs):
        page.insert_text((72, 100 + i * 80), text, fontsize=12)
    doc.save(str(path))
    doc.close()


def test_is_horizontal():
    assert is_horizontal({"dir": (1.0, 0.0)})
    assert not is_horizontal({"dir": (0.0, 1.0)})
    assert is_horizontal({})  # missing dir defaults to horizontal


def test_join_lines_rebuilds_hyphenated_words():
    assert FastPdfTranslator._join_lines(["exam-", "ple word"]) == "example word"


def test_join_lines_keeps_hyphen_before_uppercase():
    assert FastPdfTranslator._join_lines(["UPPER-", "Case"]) == "UPPER- Case"


def test_join_lines_joins_with_spaces():
    assert FastPdfTranslator._join_lines(["one", "two", "three"]) == "one two three"


def test_count_pdf_paragraphs(tmp_path):
    pdf = tmp_path / "doc.pdf"
    make_pdf(pdf)
    assert count_pdf_paragraphs(str(pdf)) == 2


def test_count_pdf_paragraphs_bad_file_returns_zero(tmp_path):
    assert count_pdf_paragraphs(str(tmp_path / "missing.pdf")) == 0


def test_translate_pdf_end_to_end(tmp_path):
    src = tmp_path / "doc.pdf"
    out = tmp_path / "doc_es.pdf"
    make_pdf(src)
    pages, saves = [], []

    FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(out),
        underlying_translation=FakeTranslation(),
        on_page=lambda done, total: pages.append((done, total)),
        on_save=lambda: saves.append(True),
    ).translate_pdf()

    assert out.exists()
    assert pages == [(1, 1)]
    assert saves == [True]
    doc = fitz.open(str(out))
    text = doc.load_page(0).get_text()
    doc.close()
    assert "HELLO WORLD" in text
    assert "SECOND PARAGRAPH" in text


def test_translate_pdf_honours_cancellation(tmp_path):
    src = tmp_path / "doc.pdf"
    make_pdf(src)
    translator = FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(tmp_path / "out.pdf"),
        underlying_translation=FakeTranslation(),
        is_cancelled=lambda: True,
    )
    with pytest.raises(CancelledError):
        translator.translate_pdf()
    assert not (tmp_path / "out.pdf").exists()
