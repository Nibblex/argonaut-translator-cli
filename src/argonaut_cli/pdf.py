"""PDF translation: fixed version of the argos-translate-files
PdfTranslator (whole paragraphs, progress and cancellation)."""

import pymupdf as fitz
from argostranslatefiles.formats.pdf import PdfTranslator

from argonaut.translation import CancelledError


def is_horizontal(line):
    return tuple(line.get("dir", (1, 0))) == (1.0, 0.0)


def count_pdf_paragraphs(file_path):
    """Counts the paragraphs that will be translated in a PDF (blocks with
    at least one horizontal text line), so a real percentage can be shown."""
    try:
        doc = fitz.open(file_path)
        count = 0
        for page_num in range(doc.page_count):
            for block in doc.load_page(page_num).get_text("dict")["blocks"]:
                count += any(
                    is_horizontal(line)
                    and any(s.get("text", "").strip() for s in line["spans"])
                    for line in block.get("lines", [])
                )
        doc.close()
        return count
    except Exception:  # noqa: BLE001
        return 0


class FastPdfTranslator(PdfTranslator):
    """Fixed version of the library's PdfTranslator: applies redactions
    once per page instead of once per chunk (the base class reprocesses
    the whole page for every chunk, which freezes the application on
    reaching 100%), reports rebuild progress and honours cancellation in
    every phase."""

    def __init__(
        self,
        pdf_path,
        output_path,
        underlying_translation,
        on_page=None,
        on_save=None,
        is_cancelled=None,
    ):
        super().__init__(pdf_path, output_path, underlying_translation)
        self._on_page = on_page or (lambda done, total: None)
        self._on_save = on_save or (lambda: None)
        self._is_cancelled = is_cancelled or (lambda: False)

    def translate_pdf(self):
        self._extract_text_from_pages()
        self._translate_pages_data()
        self._apply_translations_to_pdf()
        self._on_save()
        self._save_translated_pdf()

    def _extract_text_with_pymupdf(self, page_num: int):
        """Unlike the base class, extracts whole paragraphs (blocks) instead
        of loose chunks: the engine receives sentences with context and each
        paragraph is reinserted with a single font size. Rotated text
        (vertical watermarks) is left untouched on the page."""
        while len(self.pages_data) <= page_num:
            self.pages_data.append([])

        page = self.doc.load_page(page_num)
        for block in page.get_text("dict")["blocks"]:
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
        # without the base class's "except Exception", which silently turned
        # any error (including cancellation) into an untranslated PDF
        for page_blocks in self.pages_data:
            for block in page_blocks:
                block[2] = self.underlying_translation.translate(block[0])

    def _apply_translations_to_pdf(self):
        total = len(self.pages_data)
        for page_index, blocks in enumerate(self.pages_data):
            if self._is_cancelled():
                raise CancelledError()
            if blocks:
                self._apply_page(page_index, blocks)
            self._on_page(page_index + 1, total)

    def _apply_page(self, page_index, blocks):
        page = self.doc.load_page(page_index)

        entries = []
        for block in blocks:
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
            entries.append((block, (x0, y0, x1, y1)))

        # a single redaction pass per page
        for _block, coords in entries:
            page.add_redact_annot(fitz.Rect(*coords))
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        except Exception:  # noqa: BLE001
            for _block, coords in entries:
                page.draw_rect(fitz.Rect(*coords), color=(1, 1, 1), fill=(1, 1, 1))

        normal_blocks = []
        bold_blocks = []
        for block, coords in entries:
            is_bold = len(block) > 6 and block[6]
            (bold_blocks if is_bold else normal_blocks).append((block, coords))
        self._insert_styled_text_blocks(page, normal_blocks, is_bold=False)
        self._insert_styled_text_blocks(page, bold_blocks, is_bold=True)
