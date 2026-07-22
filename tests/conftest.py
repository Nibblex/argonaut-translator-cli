"""Shared test setup: run Qt headless and keep QSettings away from the
user's real configuration. Both must happen before any Qt import."""

import os
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="argonaut-tests-")

import pytest

from argonaut import i18n


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


@pytest.fixture(autouse=True)
def reset_language():
    """Each test starts from the default interface language."""
    i18n.set_language(i18n.DEFAULT)
    yield
    i18n.set_language(i18n.DEFAULT)


@pytest.fixture(autouse=True)
def no_blocking_dialogs(monkeypatch):
    """Modal dialogs wait for a click that never comes, so an unpatched one
    hangs the run forever (and with it CI). Fail loudly instead; tests that
    expect a dialog patch these themselves, overriding this guard."""
    from PyQt5.QtWidgets import QDialog, QFileDialog, QMessageBox

    def blocked(name):
        def guard(*args, **kwargs):
            raise AssertionError(f"unpatched modal dialog: {name}")

        return guard

    for cls, names in (
        (QMessageBox, ["question", "warning", "information", "critical", "about"]),
        (
            QFileDialog,
            ["getOpenFileName", "getOpenFileNames", "getSaveFileName",
             "getExistingDirectory"],
        ),
        (QDialog, ["exec_"]),
    ):
        for name in names:
            method = blocked(f"{cls.__name__}.{name}")
            monkeypatch.setattr(
                cls, name, method if cls is QDialog else staticmethod(method)
            )


@pytest.fixture(autouse=True)
def clean_qsettings():
    """Each test starts with empty settings: closing a window persists its
    state (qtbot closes them on teardown), which would leak across tests."""
    from PyQt5.QtCore import QSettings

    settings = QSettings()
    settings.clear()
    settings.sync()
    yield
