"""Shared test setup: keep the translation cache, the history, the
downloaded model and anything the engines write away from the user's real
files, and stand-ins for the engines so no model is ever needed."""

import os
import tempfile

_sandbox = tempfile.mkdtemp(prefix="argonaut-cli-tests-")
os.environ["XDG_CONFIG_HOME"] = _sandbox
# without this the cache DB under ~/.local/share is read *and written* by the
# suite, so tests see whatever the developer happens to have translated
os.environ["XDG_DATA_HOME"] = _sandbox

import pytest


class FakeLanguage:
    """Stands in for argostranslate's Language in tests."""

    def __init__(self, code, name, translations=()):
        self.code = code
        self.name = name
        self._translations = dict(translations)

    def __str__(self):
        return self.name

    def get_translation(self, to):
        return self._translations.get(to.code)


class FakeTranslation:
    """Stands in for an ITranslation; uppercases instead of translating."""

    def __init__(self, from_lang=None, to_lang=None):
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.calls = 0

    def translate(self, text):
        self.calls += 1
        return text.upper()


class FakeBatchTranslation(FakeTranslation):
    """FakeTranslation plus the translate_many a batching engine (NLLB)
    offers, recording the texts of each batched call."""

    def __init__(self, from_lang=None, to_lang=None):
        super().__init__(from_lang, to_lang)
        self.batches = []

    def translate_many(self, texts):
        self.calls += len(texts)
        self.batches.append(list(texts))
        return [text.upper() for text in texts]


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Each test gets its own data directory. The translation cache is a
    file under it, so without this a text translated by one test would
    still be cached for the next one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
