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


class ProgressTranslation:
    """Wraps an ITranslation to report progress chunk by chunk, cache
    repeated texts and abort mid-file."""

    def __init__(self, inner, on_progress, is_cancelled):
        self._inner = inner
        self._on_progress = on_progress
        self._is_cancelled = is_cancelled
        self._cache = {}
        self._done = 0

    def translate(self, text):
        if self._is_cancelled():
            raise CancelledError()
        result = self._cache.get(text)
        if result is None:
            # chunks with no letters (numbers, punctuation) are left as-is
            if any(c.isalpha() for c in text):
                result = self._inner.translate(text)
            else:
                result = text
            self._cache[text] = result
        self._done += 1
        self._on_progress(self._done)
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)
