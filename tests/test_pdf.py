import types

import pymupdf as fitz
import pytest

from argonaut.pdf import FastPdfTranslator, is_horizontal
from argonaut.translation import CancelledError
from tests.conftest import FakeBatchTranslation, FakeTranslation


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


def test_translate_pdf_end_to_end(tmp_path):
    src = tmp_path / "doc.pdf"
    out = tmp_path / "doc_es.pdf"
    make_pdf(src)
    pages, saves, counts = [], [], []

    FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(out),
        underlying_translation=FakeTranslation(),
        on_count_ready=counts.append,
        on_page=lambda done, total: pages.append((done, total)),
        on_save=lambda: saves.append(True),
    ).translate_pdf()

    assert out.exists()
    assert counts == [2]  # the two paragraphs, counted while parsing
    assert pages == [(1, 1)]
    assert saves == [True]
    doc = fitz.open(str(out))
    text = doc.load_page(0).get_text()
    doc.close()
    assert "HELLO WORLD" in text
    assert "SECOND PARAGRAPH" in text


def test_pdf_batches_each_page_into_one_engine_call(tmp_path):
    """A page's paragraphs go to a batching engine together: NLLB amortizes
    its cost over a batch, and per-paragraph calls would forfeit that."""
    src = tmp_path / "doc.pdf"
    out = tmp_path / "doc_es.pdf"
    make_pdf(src)
    inner = FakeBatchTranslation()

    FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(out),
        underlying_translation=inner,
    ).translate_pdf()

    assert inner.batches == [["Hello world", "Second paragraph"]]
    doc = fitz.open(str(out))
    text = doc.load_page(0).get_text()
    doc.close()
    assert "HELLO WORLD" in text


def test_every_long_phase_reports_which_page_it_is_on(tmp_path):
    """Reading a long book takes the better part of a minute, and a page of
    it barely moves the percentage: without a page number the window has no
    way to show that the run is alive."""
    src = tmp_path / "doc.pdf"
    make_pdf(src)
    reads, translated, rebuilt = [], [], []

    FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(tmp_path / "doc_es.pdf"),
        underlying_translation=FakeTranslation(),
        on_read=lambda done, total: reads.append((done, total)),
        on_translated_page=lambda done, total: translated.append((done, total)),
        on_page=lambda done, total: rebuilt.append((done, total)),
    ).translate_pdf()

    assert reads == [(1, 1)]  # the phase the base class went through in silence
    assert translated == [(1, 1)]
    assert rebuilt == [(1, 1)]


def test_vertical_text_is_left_untouched(tmp_path):
    """Rotated text (a vertical watermark) stays on the page as it was:
    translating it out of context and reinserting it horizontally would
    ruin the layout for no gain."""
    src = tmp_path / "doc.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Horizontal paragraph", fontsize=12)
    page.insert_text((72, 200), "   ")  # a blank line: never reaches the engine
    page.insert_text((300, 400), "WATERMARK", fontsize=12, rotate=90)
    doc.save(str(src))
    doc.close()

    out = tmp_path / "doc_es.pdf"
    inner = FakeTranslation()
    FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(out),
        underlying_translation=inner,
    ).translate_pdf()

    assert inner.calls == 1  # only the horizontal paragraph was translated
    doc = fitz.open(str(out))
    text = doc.load_page(0).get_text()
    doc.close()
    assert "HORIZONTAL PARAGRAPH" in text
    assert "WATERMARK" in text  # still on the page, untranslated


def test_blank_pages_are_reported_but_not_translated(tmp_path):
    src = tmp_path / "doc.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "Only page with text", fontsize=12)
    doc.new_page()  # a blank page
    doc.save(str(src))
    doc.close()

    translated = []
    inner = FakeTranslation()
    FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(tmp_path / "out.pdf"),
        underlying_translation=inner,
        on_translated_page=lambda done, total: translated.append((done, total)),
    ).translate_pdf()

    assert translated == [(1, 2), (2, 2)]  # the blank page still shows progress
    assert inner.calls == 1


def test_bold_paragraphs_survive_the_rebuild(tmp_path):
    src = tmp_path / "doc.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Regular paragraph", fontsize=12, fontname="helv")
    page.insert_text((72, 200), "Bold heading", fontsize=12, fontname="hebo")
    doc.save(str(src))
    doc.close()

    out = tmp_path / "doc_es.pdf"
    FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(out),
        underlying_translation=FakeTranslation(),
    ).translate_pdf()

    doc = fitz.open(str(out))
    spans = [
        span
        for block in doc.load_page(0).get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
    ]
    doc.close()
    weight = {span["text"]: bool(span["flags"] & 2**4) for span in spans}
    assert weight["REGULAR PARAGRAPH"] is False
    assert weight["BOLD HEADING"] is True


def test_blank_spans_never_skew_a_paragraph_style(tmp_path):
    """A span of pure whitespace carries no visible style: letting it vote
    would let an invisible 30-point bold space set the whole paragraph."""
    src = tmp_path / "doc.pdf"
    make_pdf(src)
    translator = FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(tmp_path / "out.pdf"),
        underlying_translation=FakeTranslation(),
    )

    class FakePage:
        def get_text(self, kind):
            return {"blocks": [{
                "lines": [{
                    "dir": (1.0, 0.0),
                    "bbox": (10, 10, 100, 22),
                    "spans": [
                        {"text": "Hello", "size": 12.0, "color": 0, "flags": 0},
                        {"text": "   ", "size": 30.0, "color": 5, "flags": 2**4},
                    ],
                }],
            }]}

    translator.doc.close()
    translator.doc = types.SimpleNamespace(load_page=lambda n: FakePage())
    translator.pages_data = []
    translator._extract_text_with_pymupdf(0)

    [[text, _, _, _, _, _, is_bold, size, _]] = translator.pages_data[0]
    assert text == "Hello"
    assert size == 12.0 and is_bold is False  # the blank span never counted


def test_redaction_failure_falls_back_to_covering_rectangles(tmp_path, monkeypatch):
    """Some malformed PDFs make apply_redactions raise; the original text is
    then covered with filled rectangles so the translation still lands."""
    src = tmp_path / "doc.pdf"
    out = tmp_path / "doc_es.pdf"
    make_pdf(src)

    def broken(self, *args, **kwargs):
        raise RuntimeError("bad xref")

    monkeypatch.setattr(fitz.Page, "apply_redactions", broken)
    FastPdfTranslator(
        pdf_path=str(src),
        output_path=str(out),
        underlying_translation=FakeTranslation(),
    ).translate_pdf()

    assert out.exists()
    doc = fitz.open(str(out))
    text = doc.load_page(0).get_text()
    doc.close()
    assert "HELLO WORLD" in text


def test_insertion_rect_grants_a_minimum_usable_height():
    # a 4-point-tall box cannot hold any text: the rect is recentred on the
    # original line and given 10 points
    block = ["short", (10, 100, 60, 104), "SHORT", 0, "#000000", 0, False, 12, None]
    x0, y0, x1, y1 = FastPdfTranslator._insertion_rect(block)
    assert y1 - y0 == 10
    assert (y0 + y1) / 2 == pytest.approx(102)  # the original centre


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
