"""Batch translation: a list of documents into one target language.

Language detection per file, a cache shared across the batch, skipping
outputs that already exist, the translation history and cooperative
cancellation, with no interface attached: what happens is reported to a
:class:`Listener`. The window turns each report into a Qt signal; the
command line prints it."""

import os
import time

from argostranslatefiles import argostranslatefiles

from argonaut_cli import nllb, packages
from argonaut_cli.history import TranslationHistory
from argonaut_cli.pdf import FastPdfTranslator
from argonaut_cli.translation import (
    SUPPORTED_EXTS,
    CancelledError,
    ProgressTranslation,
    TranslationCache,
    detect_language,
)


class FileError(Exception):
    """A document the batch turned down, for a reason it can name.

    ``str()`` is an English sentence, which is what the command line prints;
    an interface in another language renders the attributes instead."""

    def __init__(self, path):
        super().__init__(path)
        self.path = path

    @property
    def name(self):
        return os.path.basename(self.path)


class LanguageNotDetected(FileError):
    def __str__(self):
        return f"{self.name}: could not detect the language"


class AlreadyInLanguage(FileError):
    def __init__(self, path, lang):
        super().__init__(path)
        self.lang = lang

    def __str__(self):
        return f"{self.name}: the document is already in {self.lang}"


class NoModel(FileError):
    def __init__(self, path, src, dst):
        super().__init__(path)
        self.src = src
        self.dst = dst

    def __str__(self):
        return f"{self.name}: no model {self.src} → {self.dst}"


class Listener:
    """What a batch reports while it runs. Every method does nothing here,
    so an interface overrides only what it shows. Languages are passed as
    the engine's Language objects: naming them is the interface's job."""

    def on_file_started(self, index, path):
        pass

    def on_language_detected(self, index, lang):
        pass

    def on_progress(self, done, total):
        """Chunks of the current file done, out of ``total`` (0 while the
        file has not said how much work it holds)."""

    def on_phase(self, key, kwargs):
        """A long phase of a PDF: "reading", "translating_page",
        "generating" (with the page as ``done`` of ``total``) or "saving".
        ``kwargs`` always carries the file ``name``."""

    def on_file_done(self, index, out_path, seconds):
        pass

    def on_file_failed(self, index, error):
        """``error`` is the exception: a :class:`FileError` for a document
        the batch turned down, anything else for one that broke."""

    def on_file_skipped(self, index, out_path):
        pass

    def on_cache_stats(self, index, file_reused, batch_reused, batch_segments):
        """Segments this file served from the cache, the batch's reuse so
        far and how many segments that reuse is out of."""


def documents_in(paths):
    """The supported documents among ``paths``, folders walked recursively
    in name order and duplicates dropped, the way the window adds them."""
    seen = set()
    for path in paths:
        if os.path.isdir(path):
            found = []
            for root, dirs, files in os.walk(path):
                dirs.sort()
                found.extend(os.path.join(root, name) for name in sorted(files))
        else:
            found = [path]
        for doc in found:
            if os.path.splitext(doc)[1].lower() in SUPPORTED_EXTS and doc not in seen:
                seen.add(doc)
                yield doc


class Batch:
    """Translates ``files`` into ``dst_lang``. With ``src_lang`` None each
    file's language is detected separately. Segments repeated across the
    batch are served from one shared cache, and with ``skip_existing`` a
    file whose output is already there is left alone.

    The cache and the history are opened by :meth:`run`, not here: opening
    either prunes its expired entries, which is work for the thread that
    translates rather than for whoever sets the batch up. A path of None
    keeps that one in memory only (the cache) or off (the history)."""

    PROGRESS_EVERY = 0.2  # seconds between progress reports within a file

    def __init__(self, src_lang, dst_lang, languages, files, output_dir=None,
                 skip_existing=False, engine_id="argos", greedy=False,
                 cache_db_path=None, cache_ttl_days=0,
                 history_db_path=None, history_ttl_days=0,
                 listener=None, is_cancelled=None):
        self.src_lang = src_lang
        self.dst_lang = dst_lang
        self.languages = languages
        self.files = files
        self.output_dir = output_dir  # None = next to each original
        self.skip_existing = skip_existing
        self.engine_id = engine_id
        self.greedy = greedy
        self._cache_db_path = cache_db_path
        self._cache_ttl_days = cache_ttl_days
        self._history_db_path = history_db_path
        self._history_ttl_days = history_ttl_days
        self.listener = listener or Listener()
        self.is_cancelled = is_cancelled or (lambda: False)
        self.cache = None
        self.history = None
        self._last_progress = 0.0
        self._current_name = ""
        self._total_chunks = 0  # 0 until the file says how much work it holds
        self._batch_segments = 0  # cacheable segments seen across the batch
        self._pkg_versions = None  # pair -> version, read once per batch

    def output_path_for(self, to_code, file_path):
        # same naming scheme as the library, but honouring the output
        # folder if one was chosen
        name, ext = os.path.splitext(os.path.basename(file_path))
        dir_path = self.output_dir or os.path.dirname(file_path)
        return os.path.join(dir_path, f"{name}_{to_code}{ext}")

    def get_output_path(self, underlying_translation, file_path):
        return self.output_path_for(underlying_translation.to_lang.code, file_path)

    def expected_output_path(self, file_path):
        # the target is always the destination language, whatever the source
        # turns out to be, so the skip check needs no language detection
        return self.output_path_for(self.dst_lang.code, file_path)

    def resolve_translation(self, index, path):
        src = self.src_lang
        if src is None:
            src = detect_language(path, self.languages)
            if src is None:
                raise LanguageNotDetected(path)
            self.listener.on_language_detected(index, src)
        if src is self.dst_lang:
            raise AlreadyInLanguage(path, self.dst_lang)
        translation = src.get_translation(self.dst_lang)
        if translation is None:
            raise NoModel(path, src, self.dst_lang)
        return translation

    def engine_version(self, translation):
        """Version tag for the cache namespace, so entries produced by an
        older model stop being served after a package upgrade."""
        if self.engine_id != "argos":
            version = nllb.MODEL_REPO  # one fixed model, replaced only by us
        else:
            if self._pkg_versions is None:
                self._pkg_versions = packages.installed_versions()
            pair = (translation.from_lang.code, translation.to_lang.code)
            version = self._pkg_versions.get(pair, "")
        # greedy output is not beam output: the modes never serve each
        # other's entries, and quality-mode keys stay as they always were
        return f"{version}+greedy" if self.greedy else version

    def run(self):
        self.cache = TranslationCache(self._cache_db_path, ttl_days=self._cache_ttl_days)
        self.history = TranslationHistory(
            self._history_db_path, ttl_days=self._history_ttl_days
        )
        try:
            self._translate_files()
        finally:
            # a cancelled batch has to let go of its database connection and
            # of the segments it has translated so far, just like a finished one
            self.cache.close()
            self.history.close()

    def _translate_files(self):
        for i, path in enumerate(self.files):
            if self.is_cancelled():
                break
            self.listener.on_file_started(i, path)
            self._current_name = os.path.basename(path)
            file_start = time.monotonic()
            try:
                if self.skip_existing:
                    existing = self.expected_output_path(path)
                    if os.path.exists(existing):
                        self.listener.on_file_skipped(i, existing)
                        continue
                translation = self.resolve_translation(i, path)
                self._total_chunks = 0
                self.listener.on_progress(0, 0)
                proxy = ProgressTranslation(
                    translation,
                    self._report_progress,
                    self.is_cancelled,
                    cache=self.cache,
                    engine_id=self.engine_id,
                    engine_version=self.engine_version(translation),
                )
                if path.lower().endswith(".pdf"):
                    out = self.get_output_path(translation, path)
                    FastPdfTranslator(
                        pdf_path=path,
                        output_path=out,
                        underlying_translation=proxy,
                        on_count_ready=self._report_total,
                        on_read=self._report_read,
                        on_translated_page=self._report_translated_page,
                        on_page=self._report_page,
                        on_save=self._report_save,
                        is_cancelled=self.is_cancelled,
                    ).translate_pdf()
                else:
                    out = argostranslatefiles.translate_file(
                        proxy, path, get_output_path=self.get_output_path
                    )
                self._batch_segments += proxy.segments
                self.listener.on_cache_stats(
                    i, proxy.reused, self.cache.reused, self._batch_segments
                )
                elapsed = time.monotonic() - file_start
                self.history.record(
                    path, out or "",
                    translation.from_lang.code, translation.to_lang.code,
                    self.engine_id, elapsed,
                    reused=proxy.reused, segments=proxy.segments,
                )
                self.listener.on_file_done(i, out or "", elapsed)
            except CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.listener.on_file_failed(i, exc)

    def _report_total(self, total):
        """How many chunks the current file holds, known once it is parsed."""
        self._total_chunks = total
        self.listener.on_progress(0, total)

    def _report_progress(self, done):
        now = time.monotonic()
        total = self._total_chunks
        if done == total or now - self._last_progress > self.PROGRESS_EVERY:
            self._last_progress = now
            self.listener.on_progress(done, total)

    def _report_pages(self, key, done, total, move_bar=True):
        """Says which page of the current file a phase is on. On a long book
        the percentage can sit on the same figure for a minute, so the page
        number is what tells the user the run is alive."""
        if move_bar:
            self.listener.on_progress(done, total)
        self.listener.on_phase(
            key, {"name": self._current_name, "done": done, "total": total}
        )

    def _report_read(self, done, total):
        # reading is preparation: moving the bar here would sweep it to 100%
        # and send it back to 0% when the translation itself starts, so the
        # page number goes in the message and the bar stays indeterminate
        self._report_pages("reading", done, total, move_bar=False)

    def _report_translated_page(self, done, total):
        # the bar is already counting paragraphs, a finer measure than pages
        self._report_pages("translating_page", done, total, move_bar=False)

    def _report_page(self, done, total):
        self._report_pages("generating", done, total)

    def _report_save(self):
        self.listener.on_progress(0, 0)
        self.listener.on_phase("saving", {"name": self._current_name})
