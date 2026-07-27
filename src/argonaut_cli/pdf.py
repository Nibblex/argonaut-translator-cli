"""PDF translation: fixed version of the argos-translate-files
PdfTranslator (whole paragraphs, progress, cancellation and its own
typesetting)."""

import pymupdf as fitz
from argostranslatefiles.formats.pdf import PdfTranslator

from argonaut.translation import check_cancelled
from argonaut.typesetting import Typesetter


def is_horizontal(line):
    return tuple(line.get("dir", (1, 0))) == (1.0, 0.0)


class FastPdfTranslator(PdfTranslator):
    """Fixed version of the library's PdfTranslator: applies redactions
    once per page instead of once per chunk (the base class reprocesses
    the whole page for every chunk, which freezes the application on
    reaching 100%), reports rebuild progress, honours cancellation in
    every phase, and typesets the translation itself instead of handing
    each paragraph to insert_htmlbox (see argonaut.typesetting)."""

    def __init__(
        self,
        pdf_path,
        output_path,
        underlying_translation,
        on_count_ready=None,
        on_read=None,
        on_translated_page=None,
        on_page=None,
        on_save=None,
        is_cancelled=None,
    ):
        super().__init__(pdf_path, output_path, underlying_translation)
        self._on_count_ready = on_count_ready or (lambda n: None)
        self._on_read = on_read or (lambda done, total: None)
        self._on_translated_page = on_translated_page or (lambda done, total: None)
        self._on_page = on_page or (lambda done, total: None)
        self._on_save = on_save or (lambda: None)
        self._is_cancelled = is_cancelled or (lambda: False)
        self.typesetter = Typesetter()
        # paragraphs no size would fit into their box; kept so a caller can
        # tell a rebuild that silently lost text from one that did not
        self.dropped = 0

    def translate_pdf(self):
        phases = (
            self._extract_text_from_pages,
            self._report_count,
            self._translate_pages_data,
            self._apply_translations_to_pdf,
            self._on_save,
            self._save_translated_pdf,
        )
        try:
            for phase in phases:
                self._abort_if_cancelled()  # every phase is a cancel point
                phase()
        finally:
            # the base class only closes the document on the success path, so
            # a cancelled PDF would hold its pages until the process ends
            if not self.doc.is_closed:
                self.doc.close()
            self.pages_data = []

    def _abort_if_cancelled(self):
        check_cancelled(self._is_cancelled)

    def _cancellable(self, items):
        """Yields ``items``, giving cancellation a chance between each one."""
        for item in items:
            self._abort_if_cancelled()
            yield item

    def _extract_text_from_pages(self):
        # the base class reads every page in silence; on a long book that is
        # the better part of a minute in which the window has nothing to show
        total = self.doc.page_count
        for page_num in range(total):
            self._abort_if_cancelled()
            self._extract_text_with_pymupdf(page_num)
            self._on_read(page_num + 1, total)

    def _report_count(self):
        # the count comes from the already-extracted pages_data, so the PDF
        # is never parsed a second time just to fill the progress bar
        self._on_count_ready(sum(len(page) for page in self.pages_data))

    def _extract_text_with_pymupdf(self, page_num: int):
        """Unlike the base class, extracts whole paragraphs (blocks) instead
        of loose chunks: the engine receives sentences with context and each
        paragraph is reinserted with a single font size. Rotated text
        (vertical watermarks) is left untouched on the page."""
        while len(self.pages_data) <= page_num:
            self.pages_data.append([])

        page = self.doc.load_page(page_num)
        for block in self._cancellable(page.get_text("dict")["blocks"]):
            lines = []
            rect = None
            sizes = {}
            colors = {}
            bold_chars = 0
            total_chars = 0
            for line in block.get("lines", []):
                if not is_horizontal(line):
                    continue
                text = "".join(s.get("text", "") for s in line["spans"]).strip()
                if not text:
                    continue
                lines.append(text)
                line_rect = fitz.Rect(line["bbox"])
                rect = line_rect if rect is None else rect | line_rect
                for span in line["spans"]:
                    span_text = span.get("text", "").strip()
                    if not span_text:
                        continue
                    weight = len(span_text)
                    total_chars += weight
                    size = round(span.get("size", 12), 1)
                    sizes[size] = sizes.get(size, 0) + weight
                    color = span.get("color", 0)
                    colors[color] = colors.get(color, 0) + weight
                    if span.get("flags", 0) & 2**4:
                        bold_chars += weight
            if not lines:
                continue

            self.pages_data[page_num].append([
                self._join_lines(lines),
                tuple(rect),
                None,  # translation pending
                0,
                self._decimal_to_hex_color(max(colors, key=colors.get)),
                0,
                bold_chars > total_chars / 2,
                max(sizes, key=sizes.get),  # dominant font size of the paragraph
                None,  # per-chunk links are not preserved
            ])

    @staticmethod
    def _join_lines(lines):
        """Joins a paragraph's lines, rebuilding the words the original
        split with a hyphen at the end of a line."""
        text = lines[0]
        for line in lines[1:]:
            if text.endswith("-") and line[:1].islower():
                text = text[:-1] + line
            else:
                text += " " + line
        return text

    def _translate_pages_data(self):
        # process one page at a time: all paragraphs on the page are batched
        # into a single engine call (preserving the speedup) while progress
        # is reported once per page so the bar visibly advances
        total = len(self.pages_data)
        for page_index, page_blocks in enumerate(self._cancellable(self.pages_data)):
            # the page number is reported even for empty pages: on a big book
            # the percentage barely moves, and it is the only sign of life
            self._on_translated_page(page_index + 1, total)
            if not page_blocks:
                continue
            texts = [block[0] for block in page_blocks]
            # a plain ITranslation (no batching) is still accepted, so the
            # class stays usable outside the worker
            translate_many = getattr(
                self.underlying_translation, "translate_many", None
            )
            if translate_many is not None:
                translated = translate_many(texts)
            else:
                translated = [
                    self.underlying_translation.translate(text) for text in texts
                ]
            for block, result in zip(page_blocks, translated):
                block[2] = result

    def _apply_translations_to_pdf(self):
        total = len(self.pages_data)
        for page_index, blocks in enumerate(self._cancellable(self.pages_data)):
            if blocks:
                self._apply_page(page_index, blocks)
            self._on_page(page_index + 1, total)

    def _apply_page(self, page_index, blocks):
        page = self.doc.load_page(page_index)

        rects = []  # every paragraph, in page order, for the redaction pass
        by_weight = {False: [], True: []}  # and grouped for the insert passes
        for block in self._cancellable(blocks):
            coords = self._insertion_rect(block)
            rects.append(coords)
            by_weight[bool(block[6])].append((block, coords))

        # a single redaction pass per page
        for coords in self._cancellable(rects):
            page.add_redact_annot(fitz.Rect(*coords))
        self._abort_if_cancelled()  # outside the try: CancelledError is an
        try:                        # Exception, and would be swallowed below
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        except Exception:  # noqa: BLE001
            for coords in self._cancellable(rects):
                page.draw_rect(fitz.Rect(*coords), color=(1, 1, 1), fill=(1, 1, 1))

        # a page at a time: the typesetter shares one writer per colour across
        # everything it is given, and writing a page now costs milliseconds,
        # so there is nothing left to interrupt inside one
        for is_bold, entries in by_weight.items():
            self._abort_if_cancelled()
            self.dropped += self.typesetter.write(page, entries, bold=is_bold)

    def _save_translated_pdf(self):
        """Writes the rebuilt document.

        This used to be the slow part: insert_htmlbox embedded a copy of its
        font on every call, and ``garbage=4`` finds duplicates by comparing
        objects against each other, so a 528-page book spent 223 s here
        collapsing a dozen objects per paragraph. Typesetting the text
        ourselves left one font for the whole document — three descriptors
        in a 79-page book — and the same save now takes under a second.

        Both settings stay because they still pay for themselves at no
        cost: subsetting trims the embedded faces to the glyphs actually
        used, and garbage=4 is no slower than the lower levels now that it
        has little to collapse. The base class copies the document into a
        fresh one before saving, which costs the same and drops the
        original's metadata and outline, so the working document is saved
        directly instead."""
        try:
            self.doc.subset_fonts()
        except Exception:  # noqa: BLE001
            pass  # an optimisation: a font it cannot subset must not fail the save
        self.doc.save(self.output_path, garbage=4, deflate=True)

    @staticmethod
    def _insertion_rect(block):
        """Where the translation goes: the original box, widened a little for
        the text that grew and given a minimum usable height."""
        translated_text = block[2] if block[2] is not None else block[0]
        len_ratio = min(
            1.05, max(1.01, len(translated_text) / max(1, len(block[0])))
        )
        x0, y0, x1, y1 = block[1]
        x1 += (len_ratio - 1) * (x1 - x0)
        vertical_margin = min((y1 - y0) * 0.1, 3)
        y0 += vertical_margin
        y1 -= vertical_margin
        if y1 - y0 < 10:
            y_center = (block[1][1] + block[1][3]) / 2
            y0, y1 = y_center - 5, y_center + 5
        return x0, y0, x1, y1
