"""Translation utilities: language detection, supported formats and the
ITranslation wrapper that reports progress and allows cancelling."""

from argostranslatefiles import argostranslatefiles
from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException

DetectorFactory.seed = 0

# langdetect codes that don't match the Argos ones
LANGDETECT_TO_ARGOS = {"zh-cn": "zh", "zh-tw": "zt"}


class CancelledError(Exception):
    pass


def detect_language(file_path, languages):
    """Detects a file's language and returns the installed Language, or None."""
    sample = None
    for fmt in argostranslatefiles.get_supported_formats():
        if fmt.support(file_path):
            sample = fmt.get_texts(file_path)
            break
    if not sample or not sample.strip():
        return None
    try:
        code = detect(sample)
    except LangDetectException:
        return None
    code = LANGDETECT_TO_ARGOS.get(code, code)
    for lang in languages:
        if lang.code == code:
            return lang
    return None


def supported_extensions():
    exts = set()
    for fmt in argostranslatefiles.get_supported_formats():
        exts.update(fmt.supported_file_extensions)
    return sorted(exts)


SUPPORTED_EXTS = supported_extensions()


class TranslationCache:
    """A segment cache shared across a batch, tracking how much it is reused.

    ``hits`` counts segments served without touching the engine, ``misses``
    those translated for the first time; together they let the window show
    how much a batch benefits from files sharing content."""

    def __init__(self):
        self._entries = {}
        self.hits = 0
        self.misses = 0

    def lookup(self, key):
        """Returns (value, found). A found key counts as a hit, a missing
        one as a miss, so the caller need only translate on a miss."""
        if key in self._entries:
            self.hits += 1
            return self._entries[key], True
        self.misses += 1
        return None, False

    def store(self, key, value):
        self._entries[key] = value

    @property
    def reused(self):
        """Segments served from the cache instead of being retranslated."""
        return self.hits


class ProgressTranslation:
    """Wraps an ITranslation to report progress chunk by chunk, cache
    repeated texts and abort mid-file.

    Pass a shared ``cache`` (a :class:`TranslationCache`) to reuse
    translations across a whole batch so a repeated paragraph translates
    once no matter how many files contain it. Entries are namespaced by
    language pair, so a file detected as a different source language never
    picks up another pair's translation."""

    def __init__(self, inner, on_progress, is_cancelled, cache=None):
        self._inner = inner
        self._on_progress = on_progress
        self._is_cancelled = is_cancelled
        self._cache = TranslationCache() if cache is None else cache
        self._prefix = self._language_pair()
        self._done = 0
        self.reused = 0  # segments this file served from the cache

    def _language_pair(self):
        def code(lang):
            return getattr(lang, "code", "") if lang is not None else ""

        return (
            code(getattr(self._inner, "from_lang", None)),
            code(getattr(self._inner, "to_lang", None)),
        )

    def translate(self, text):
        if self._is_cancelled():
            raise CancelledError()
        key = (self._prefix, text)
        result, found = self._cache.lookup(key)
        if found:
            self.reused += 1
        else:
            # chunks with no letters (numbers, punctuation) are left as-is
            if any(c.isalpha() for c in text):
                result = self._inner.translate(text)
            else:
                result = text
            self._cache.store(key, result)
        self._done += 1
        self._on_progress(self._done)
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)
