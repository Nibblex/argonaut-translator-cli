"""The typesetting the translated PDF gets instead of insert_htmlbox."""

import pymupdf as fitz
import pytest

from argonaut.typesetting import Typesetter, is_rtl, split_tokens, starts_rtl


def block(text, size=11, color="#000000"):
    """A paragraph as the PDF translator hands it over."""
    return [text, None, text, 0, color, 0, False, size, None]


def write(page, text, rect, size=11, color="#000000", bold=False):
    typesetter = Typesetter()
    dropped = typesetter.write(page, [(block(text, size, color), tuple(rect))], bold=bold)
    return dropped, page.get_text().strip()


@pytest.fixture
def page():
    return fitz.open().new_page(width=400, height=400)


# --- breaking text into lines ---

def test_words_are_the_units_of_a_line_break():
    assert split_tokens("one two three") == [
        ("one", False), ("two", True), ("three", True),
    ]


def test_ideographs_break_between_characters():
    """Chinese and Japanese are written without spaces, so a line that could
    only break at a space would never break at all."""
    assert split_tokens("敏捷的") == [("敏", False), ("捷", False), ("的", False)]


def test_a_word_mixing_scripts_splits_at_the_ideographs():
    assert split_tokens("PDF文書") == [("PDF", False), ("文", False), ("書", False)]


def test_a_trailing_run_of_letters_closes_the_word():
    """The tail after the last ideograph is a piece of its own, and it does
    not open a space either: 文書PDF is one word, not two."""
    assert split_tokens("x 文書PDF") == [
        ("x", False), ("文", True), ("書", False), ("PDF", False),
    ]


def test_an_empty_paragraph_is_nothing_to_write(page):
    assert Typesetter().fit("   ", fitz.Rect(0, 0, 100, 100), 11, False) is None


def test_right_to_left_is_recognised():
    assert is_rtl("الثعلب البني")
    assert is_rtl("השועל החום")
    assert not is_rtl("the quick fox")
    assert not is_rtl("Быстрая лиса")


# --- laying a paragraph into its box ---

def test_a_paragraph_is_written_into_its_box(page):
    dropped, text = write(page, "The quick brown fox", fitz.Rect(20, 20, 380, 60))
    assert dropped == 0
    assert text == "The quick brown fox"


def test_text_too_long_for_its_box_is_shrunk_to_fit(page):
    """Translations run longer than the original, and the box is the space
    the original took: without shrinking, most paragraphs would not fit."""
    largo = " ".join(["palabra"] * 40)
    caja = fitz.Rect(20, 20, 200, 60)
    dropped, text = write(page, largo, caja, size=11)
    assert dropped == 0
    assert text.split() == largo.split()
    escrito = page.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]
    assert escrito["size"] < 11  # it did not fit at the size it asked for
    assert page.get_text("blocks")[0][3] <= caja.y1 + 1  # nor did it overflow


def test_a_paragraph_that_cannot_be_shrunk_enough_is_reported(page):
    """A box too small for any size is counted rather than passed over in
    silence: text vanishing from a translation must be visible to the caller."""
    dropped, _ = write(page, " ".join(["palabra"] * 200), fitz.Rect(20, 20, 40, 30))
    assert dropped == 1


def test_the_colour_of_the_paragraph_is_kept(page):
    write(page, "Coloured heading", fitz.Rect(20, 20, 380, 60), color="#cc0000")
    span = page.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]
    assert span["color"] == 0xCC0000


def test_bold_paragraphs_get_a_bold_face(page):
    write(page, "Bold heading", fitz.Rect(20, 20, 380, 60), bold=True)
    span = page.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]
    assert "Bold" in span["font"]


# --- what the text layer says ---

def test_a_space_survives_a_change_of_font(page):
    """Each script is written with the font that has its glyphs, and a gap
    left by moving the pen reads as a space only within one font. Written as
    a character it survives the change, so the text can still be copied."""
    _, text = write(page, "▶ CPU 中文 end", fitz.Rect(20, 20, 380, 60))
    assert text == "▶ CPU 中文 end"


@pytest.mark.parametrize("muestra", [
    "Быстрая коричневая лиса",       # cirílico
    "Η γρήγορη καφέ αλεπού",         # griego
    "敏捷的棕色狐狸",                  # chino
    "素早い茶色のキツネ",               # japonés
    "השועל החום המהיר",              # hebreo
    "तेज़ भूरी लोमड़ी",                # devanagari
])
def test_every_script_is_written_with_a_font_that_has_it(page, muestra):
    """A language whose glyphs no font on the page carries would come out
    blank — the failure this fallback exists to prevent."""
    dropped, text = write(page, muestra, fitz.Rect(20, 20, 380, 80), size=14)
    assert dropped == 0
    assert text.replace(" ", "") == muestra.replace(" ", "")


def test_the_paragraph_direction_is_set_by_its_first_letter():
    """Digits and punctuation belong to whatever surrounds them, so a
    sentence opening with a number still reads in its own direction."""
    assert starts_rtl("الثعلب البني")
    assert starts_rtl("2020 كان عاما")
    assert not starts_rtl("El informe السريع")
    assert not starts_rtl("2020 was a year")
    assert not starts_rtl("2020 — 15%")  # no letter to ask: left to right


def test_latin_inside_an_arabic_paragraph_keeps_its_order(page):
    """A run written right to left is turned round; a name or a number
    inside it is not. Turning the whole line round instead spelled the name
    backwards and moved the year away from the word it belongs to."""
    _, text = write(page, "دراسة Hellwig حول الأزمة 2020", fitz.Rect(20, 20, 380, 80), size=14)
    assert "Hellwig" in text
    assert "2020" in text and "0202" not in text


def test_an_arabic_phrase_does_not_turn_its_paragraph_round(page):
    """The other way about: a Latin paragraph quoting Arabic stays Latin."""
    _, text = write(page, "El informe السريع de 2020 concluye", fitz.Rect(20, 20, 380, 80), size=14)
    assert text.startswith("El informe")
    assert text.endswith("de 2020 concluye")


def test_arabic_is_written_right_to_left(page):
    """Arabic also joins its letters up depending on where they sit, which
    MuPDF only does for a line handed over whole."""
    dropped, text = write(page, "الثعلب البني السريع", fitz.Rect(20, 20, 380, 80), size=14)
    assert dropped == 0
    assert text  # shaped forms, so not comparable to the source text
    span = page.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]
    assert "Arabic" in span["font"]


# --- pages whose content does not start at the origin ---

def cropped_page(box):
    """A page whose CropBox is written by hand, so it may reach past the
    paper the way a book with crop marks does — which set_cropbox refuses
    to build."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    doc.xref_set_key(page.xref, "CropBox", f"[{box[0]} {box[1]} {box[2]} {box[3]}]")
    return doc.reload_page(page)


def test_a_cropbox_reaching_past_the_paper_does_not_displace_the_text():
    """A book with crop marks keeps a CropBox larger than its page. What is
    visible is then the paper, while a TextWriter writes from the CropBox
    corner, so without compensating every paragraph lands offset by it and
    whatever that pushes past the edge is lost."""
    page = cropped_page((-30, -40, 430, 440))
    assert page.rect == fitz.Rect(0, 0, 400, 400)  # the paper, not the CropBox

    caja = fitz.Rect(20, 20, 300, 60)
    dropped, text = write(page, "Near the corner", caja)
    assert dropped == 0
    assert text == "Near the corner"
    escrito = fitz.Rect(page.get_text("blocks")[0][:4])
    assert escrito.x0 == pytest.approx(caja.x0, abs=2)
    assert escrito.y1 <= caja.y1 + 2


def test_a_cropbox_inside_the_paper_is_left_alone():
    """The common case: the CropBox is what the page shows, and the writer
    already works in its space. Compensating here would displace the text."""
    page = cropped_page((30, 40, 370, 360))
    caja = fitz.Rect(20, 20, 300, 60)
    dropped, text = write(page, "Inside the crop", caja)
    assert dropped == 0
    assert text == "Inside the crop"
    assert fitz.Rect(page.get_text("blocks")[0][:4]).x0 == pytest.approx(caja.x0, abs=2)
