import re
import sys
import types

import pytest

from argonaut import nllb
from argonaut.translation import CancelledError


# --- language table ---

def test_language_codes_are_unique():
    codes = [code for code, _, _ in nllb.LANGUAGES]
    assert len(codes) == len(set(codes))


def test_flores_codes_are_well_formed():
    for _, _, flores in nllb.LANGUAGES:
        assert re.fullmatch(r"[a-z]{3}_[A-Z][a-z]{3}", flores), flores


def test_common_languages_present():
    codes = {code for code, _, _ in nllb.LANGUAGES}
    assert {"en", "es", "fr", "de", "it", "pt", "zh", "zt"} <= codes


def test_get_installed_languages_share_one_engine():
    languages = nllb.get_installed_languages("/nonexistent")
    assert len({id(lang.engine) for lang in languages}) == 1
    spanish = next(lang for lang in languages if lang.code == "es")
    assert str(spanish) == "Spanish"


# --- sentence splitting ---

def test_split_sentences():
    assert nllb.split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]
    assert nllb.split_sentences("No trailing punctuation") == [
        "No trailing punctuation"
    ]
    assert nllb.split_sentences("   ") == []


# --- translation wrapper ---

class FakeEngine:
    def __init__(self):
        self.calls = []

    def translate_batch(self, sentences, src, dst):
        self.calls.append((sentences, src, dst))
        return [s.upper() for s in sentences]


def test_translation_splits_and_rejoins():
    languages = nllb.get_installed_languages("/nonexistent")
    english = next(lang for lang in languages if lang.code == "en")
    spanish = next(lang for lang in languages if lang.code == "es")
    translation = english.get_translation(spanish)
    translation.engine = FakeEngine()

    assert translation.translate("Hello. Bye.") == "HELLO. BYE."
    assert translation.engine.calls == [
        (["Hello.", "Bye."], "eng_Latn", "spa_Latn")
    ]
    assert translation.from_lang is english
    assert translation.to_lang is spanish


def test_translation_returns_blank_text_as_is():
    translation = nllb.NllbTranslation(FakeEngine(), None, None)
    assert translation.translate("  ") == "  "


def test_translation_preserves_paragraph_breaks():
    languages = nllb.get_installed_languages("/nonexistent")
    english = next(lang for lang in languages if lang.code == "en")
    spanish = next(lang for lang in languages if lang.code == "es")
    translation = english.get_translation(spanish)
    translation.engine = FakeEngine()

    assert translation.translate("One.\n\nTwo. Three.\n") == "ONE.\n\nTWO. THREE.\n"


# --- engine token assembly ---

def test_engine_builds_nllb_token_layout(monkeypatch, tmp_path):
    captured = {}

    class FakeResult:
        def __init__(self, tokens):
            self.hypotheses = [tokens]

    class FakeTranslator:
        def __init__(self, path, **kwargs):
            captured["model_path"] = path

        def translate_batch(self, source, target_prefix=None, **kwargs):
            captured["source"] = source
            captured["target_prefix"] = target_prefix
            return [FakeResult([prefix[0], "▁hola"]) for prefix in target_prefix]

    class FakeProcessor:
        def __init__(self, path):
            captured["spm_path"] = path

        def encode(self, text, out_type=str):
            return text.split()

        def decode(self, tokens):
            return " ".join(t.lstrip("▁") for t in tokens)

    monkeypatch.setitem(
        sys.modules, "ctranslate2", types.SimpleNamespace(Translator=FakeTranslator)
    )
    monkeypatch.setitem(
        sys.modules,
        "sentencepiece",
        types.SimpleNamespace(SentencePieceProcessor=FakeProcessor),
    )

    engine = nllb.NllbEngine(str(tmp_path))
    out = engine.translate_batch(["hello world"], "eng_Latn", "spa_Latn")

    assert captured["source"] == [["eng_Latn", "hello", "world", "</s>"]]
    assert captured["target_prefix"] == [["spa_Latn"]]
    assert out == ["hola"]  # the language token is stripped before decoding


# --- model installation and download ---

def test_is_model_installed(tmp_path):
    assert not nllb.is_model_installed(str(tmp_path))
    for name in nllb.MODEL_FILES:
        (tmp_path / name).write_text("x")
    assert nllb.is_model_installed(str(tmp_path))
    (tmp_path / "model.bin").write_text("")  # empty file = broken download
    assert not nllb.is_model_installed(str(tmp_path))


@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in nllb.MODEL_FILES:
        (repo / name).write_bytes(f"content of {name}".encode())
    return repo


def test_download_model(tmp_path, fake_repo):
    dest = tmp_path / "dest"
    progress = []
    nllb.download_model(
        str(dest),
        base_url=fake_repo.as_uri(),
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert nllb.is_model_installed(str(dest))
    assert (dest / "model.bin").read_text() == "content of model.bin"
    done, total = progress[-1]
    assert done == total > 0


def test_remove_model(tmp_path):
    target = tmp_path / "model"
    target.mkdir()
    (target / "model.bin").write_text("x")
    nllb.remove_model(str(target))
    assert not target.exists()
    nllb.remove_model(str(target))  # already gone: no error


def test_download_cancel_removes_partial_file(tmp_path, fake_repo):
    dest = tmp_path / "dest"
    with pytest.raises(CancelledError):
        nllb.download_model(
            str(dest), base_url=fake_repo.as_uri(), is_cancelled=lambda: True
        )
    assert not (dest / nllb.MODEL_FILES[0]).exists()
    assert not nllb.is_model_installed(str(dest))
