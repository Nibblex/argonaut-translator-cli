"""Typesetting the translated text into a PDF page.

The library inserts every paragraph with ``insert_htmlbox``, which builds a
whole Story per call: it embeds a copy of its font and leaves three form
XObjects behind each time. A book comes out holding a dozen PDF objects per
paragraph, and the deduplicating save then has to compare them against each
other, which costs the square of how many there are — 223 s of the 290 s a
528-page book took to write.

Here the text goes through a TextWriter instead, which draws into the page's
own content stream with a font stored once for the whole document. That means
doing the typesetting ourselves: breaking the lines, sizing them to the box,
and picking a font per script — Story did all three.

Measured over ten real documents: a third of the objects, files 4% to 25%
smaller, and text shrunk slightly *less* than before to fit its box. The
528-page book went from 267 s to 12 s.
"""

from itertools import groupby

import pymupdf as fitz

# The scripts the translation languages need. 0 is the default face (Latin,
# Cyrillic, Greek); the rest cover what it does not. They are the same fonts
# Story falls back to, and MuPDF carries all of them.
SCRIPTS = (0, 24, 6, 5, 9, 19)  # base, CJK, Arabic, Hebrew, Devanagari, Thai

# Written right to left, and Arabic also joins its letters up depending on
# where they sit in the word. MuPDF does both, given a whole line at once.
RTL_RANGES = (
    (0x0590, 0x05FF), (0x0600, 0x06FF), (0x0700, 0x074F),
    (0x0750, 0x077F), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF),
)

# Ideographs and kana are not spaced apart: a line may break between any two
CJK_RANGES = (
    (0x2E80, 0x303F), (0x3040, 0x30FF), (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFF9F),
)

LEADING = 1.16  # line height as a multiple of the font size
MIN_SCALE = 0.3  # how far the text may be shrunk to fit its box
SCALE_STEP = 0.05


def _in_ranges(code, ranges):
    return any(first <= code <= last for first, last in ranges)


def is_rtl(text):
    return any(_in_ranges(ord(char), RTL_RANGES) for char in text)


def is_cjk(char):
    return _in_ranges(ord(char), CJK_RANGES)


def cropbox_shift(page):
    """How far the page's visible area sits from the corner a TextWriter
    writes from.

    A writer works in the CropBox's space, while ``page.rect`` — where the
    paragraphs were found — is the part of it the MediaBox actually shows.
    They differ only in books whose CropBox reaches past the paper for crop
    marks, and there every paragraph would land displaced by that corner,
    losing whatever it pushed off the edge."""
    cropbox = page.cropbox
    visible = fitz.Rect(page.mediabox) & cropbox
    return fitz.Point(visible.x0 - cropbox.x0, visible.y1 - cropbox.y1)


def hex_to_rgb(color):
    """``#rrggbb`` as the 0..1 triple PyMuPDF wants."""
    value = color.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def split_tokens(text):
    """The pieces a line may break between: words, and each ideograph on its
    own. Each is paired with whether a space precedes it."""
    tokens = []
    for word in text.split():
        # only the first piece carries the space that stood before the word:
        # the ideographs inside it follow each other with nothing between
        spaced = True
        for cjk, run in groupby(word, is_cjk):
            # a run of letters is one piece; a run of ideographs, one each
            for piece in (list(run) if cjk else ["".join(run)]):
                tokens.append((piece, spaced))
                spaced = False
    if tokens:
        tokens[0] = (tokens[0][0], False)  # never a space to open a paragraph
    return tokens


class Typesetter:
    """Lays translated paragraphs into their boxes and writes them.

    Widths are measured once per token at size 1 and scaled arithmetically,
    since a text's width is linear in the font size. Without that the fitting
    loop would re-measure every word at every size it tries, which measured
    three times slower than the Story it replaces rather than faster."""

    def __init__(self):
        self._families = {}  # bold -> fonts, the default face first
        self._fonts = {}     # (token, bold) -> the font that can write it
        self._widths = {}    # (token, bold) -> width at size 1
        self._spaces = {}    # font -> width of a space at size 1

    def family(self, bold):
        """The faces to try for a paragraph, the one to prefer first.

        MuPDF's fallback fonts come in one weight — asking for bold returns
        the same regular face — so bold text opens with Times-Bold, which is
        a real bold covering Latin, Cyrillic and Greek. The scripts it does
        not reach (CJK, Arabic, Hebrew, Devanagari, Thai) have no bold to
        offer and keep their regular face, as the Story did before."""
        family = self._families.get(bold)
        if family is None:
            family = [fitz.Font("tibo")] if bold else []
            for script in SCRIPTS:
                try:
                    family.append(fitz.Font(script=script, is_serif=1))
                except Exception:  # noqa: BLE001
                    pass  # a script this MuPDF build does not carry
            self._families[bold] = family or [fitz.Font("helv")]
        return family

    def font_for(self, token, bold):
        """The first font of the family holding every glyph of the token."""
        key = (token, bold)
        font = self._fonts.get(key)
        if font is None:
            family = self.family(bold)
            font = next(
                (f for f in family if all(f.has_glyph(ord(c)) for c in token)),
                family[0],
            )
            self._fonts[key] = font
        return font

    def width_of(self, token, bold):
        key = (token, bold)
        width = self._widths.get(key)
        if width is None:
            width = self.font_for(token, bold).text_length(token, fontsize=1)
            self._widths[key] = width
        return width

    def space_of(self, font):
        width = self._spaces.get(font)
        if width is None:
            width = font.text_length(" ", fontsize=1)
            self._spaces[font] = width
        return width

    def _wrap(self, tokens, widths, space, box_width, size):
        """The tokens grouped into lines, or None when one cannot fit at all."""
        limit = box_width / size
        lines, current, used = [], [], 0.0
        for (token, spaced), width in zip(tokens, widths):
            if width > limit:
                return None
            gap = space if (spaced and current) else 0.0
            if used + gap + width <= limit:
                current.append((token, gap > 0))
                used += gap + width
            else:
                lines.append(current)
                current, used = [(token, False)], width
        if current:
            lines.append(current)
        return lines

    def fit(self, text, rect, size, bold):
        """The largest size up to ``size`` at which the text fits its box,
        with the lines it breaks into. None when even the smallest will not."""
        tokens = split_tokens(text)
        if not tokens:
            return None
        widths = [self.width_of(token, bold) for token, _ in tokens]
        space = self.space_of(self.family(bold)[0])
        scale = 1.0
        while scale >= MIN_SCALE:
            fitted = size * scale
            lines = self._wrap(tokens, widths, space, rect.width, fitted)
            if lines and len(lines) * fitted * LEADING <= rect.height:
                return fitted, lines
            scale -= SCALE_STEP
        return None

    def write(self, page, entries, bold):
        """Writes ``(block, coords)`` entries onto the page, returning how
        many paragraphs found no size that fits.

        One writer per colour: a TextWriter paints everything it holds in a
        single colour, and a page rarely uses more than a couple."""
        shift = cropbox_shift(page)
        offset = (shift.x, shift.y) * 2
        writers = {}
        dropped = 0
        for block, coords in entries:
            text = block[2] if block[2] is not None else block[0]
            rect = fitz.Rect(*coords) + offset
            fitted = self.fit(text, rect, block[7], bold)
            if fitted is None:
                dropped += 1
                continue
            size, lines = fitted
            writer = writers.setdefault(
                hex_to_rgb(block[4]), fitz.TextWriter(page.rect)
            )
            baseline = rect.y0 + size
            rtl = is_rtl(text)  # a property of the paragraph, not of its lines
            for line in lines:
                self._write_line(writer, line, rtl, rect, baseline, size, bold)
                baseline += size * LEADING
        for color, writer in writers.items():
            if writer.text_rect is not None:
                writer.write_text(page, color=color)
        return dropped

    def _write_line(self, writer, line, rtl, rect, baseline, size, bold):
        if rtl:
            # the whole line in one go: split up, the letters would neither
            # join nor be reordered, and the line would come out backwards
            phrase = " ".join(token for token, _ in line)
            font = self.font_for(phrase, bold)
            width = font.text_length(phrase, fontsize=size)
            writer.append(
                (max(rect.x0, rect.x1 - width), baseline), phrase,
                font=font, fontsize=size, right_to_left=1,
            )
            return
        runs = self._runs(line, bold)
        x = rect.x0
        for index, (text, font) in enumerate(runs):
            writer.append((x, baseline), text, font=font, fontsize=size)
            if index + 1 < len(runs):  # the last run's width is never needed
                x += font.text_length(text, fontsize=size)

    def _runs(self, line, bold):
        """The line grouped into the longest strings sharing a font, spaces
        included.

        A gap left by advancing the pen is only geometry: within a font the
        extractor reads it as a space, but across a font change it does not,
        and the text layer comes out with "▶CPU" where the page shows
        "▶ CPU". Writing the space as a character says it outright."""
        runs, run, run_font = [], "", None
        for token, spaced in line:
            font = self.font_for(token, bold)
            if font is not run_font and run:
                runs.append((run, run_font))
                run = ""
            run_font = font
            run += (" " if spaced else "") + token
        if run:
            runs.append((run, run_font))
        return runs
