import pytest

from argonaut.translation import (
    LANGDETECT_TO_ARGOS,
    SUPPORTED_EXTS,
    CancelledError,
    ProgressTranslation,
    detect_language,
)
from tests.conftest import FakeLanguage, FakeTranslation

ENGLISH_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "This paragraph is written in plain English so that the language "
    "detector has more than enough material to work with reliably."
)


def make_proxy(cancelled=False):
    inner = FakeTranslation()
    progress = []
    proxy = ProgressTranslation(inner, progress.append, lambda: cancelled)
    return inner, progress, proxy


def test_translates_and_reports_progress():
    inner, progress, proxy = make_proxy()
    assert proxy.translate("hello") == "HELLO"
    assert proxy.translate("world") == "WORLD"
    assert progress == [1, 2]
    assert inner.calls == 2


def test_repeated_text_is_cached_but_still_counted():
    inner, progress, proxy = make_proxy()
    assert proxy.translate("hello") == "HELLO"
    assert proxy.translate("hello") == "HELLO"
    assert inner.calls == 1
    assert progress == [1, 2]


def test_text_without_letters_is_passed_through():
    inner, _, proxy = make_proxy()
    assert proxy.translate("12 / 34") == "12 / 34"
    assert inner.calls == 0


def test_cancellation_raises():
    _, _, proxy = make_proxy(cancelled=True)
    with pytest.raises(CancelledError):
        proxy.translate("hello")


def test_unknown_attributes_delegate_to_inner():
    inner, _, proxy = make_proxy()
    inner.to_lang = "sentinel"
    assert proxy.to_lang == "sentinel"


def test_supported_extensions_include_the_expected_formats():
    for ext in (".txt", ".pdf", ".docx", ".epub", ".srt"):
        assert ext in SUPPORTED_EXTS


def test_chinese_codes_are_mapped_to_argos():
    assert LANGDETECT_TO_ARGOS["zh-cn"] == "zh"
    assert LANGDETECT_TO_ARGOS["zh-tw"] == "zt"


def test_detect_language_finds_installed_language(tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text(ENGLISH_TEXT)
    english = FakeLanguage("en", "English")
    assert detect_language(str(doc), [english]) is english


def test_detect_language_returns_none_when_not_installed(tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text(ENGLISH_TEXT)
    assert detect_language(str(doc), [FakeLanguage("es", "Spanish")]) is None


def test_detect_language_returns_none_for_empty_file(tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("   \n  ")
    assert detect_language(str(doc), [FakeLanguage("en", "English")]) is None
