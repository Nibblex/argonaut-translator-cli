"""The command line, end to end over the fake engines: what each command
prints, where, and the exit status it leaves."""

import io
import types

import argostranslate.settings
import pytest

from argonaut import cli, nllb, packages
from argonaut.history import TranslationHistory
from argonaut.translation import CancelledError, TranslationCache
from tests.conftest import FakeLanguage, FakeTranslation
from tests.test_translation import ENGLISH_TEXT


def make_langs():
    english = FakeLanguage("en", "English")
    spanish = FakeLanguage("es", "Spanish")
    english._translations["es"] = FakeTranslation(english, spanish)
    return [english, spanish]


@pytest.fixture
def langs(monkeypatch):
    """Both commands that load an engine get the fake languages instead."""
    languages = make_langs()
    monkeypatch.setattr(cli, "load_languages", lambda *args, **kwargs: languages)
    return languages


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    status = cli.main(list(argv), out=out, err=err)
    return status, out.getvalue(), err.getvalue()


# --- translate ---

def test_translate_writes_next_to_the_original_and_prints_its_path(langs, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    status, out, err = run("translate", str(doc), "--from", "en", "--to", "es")

    assert status == 0
    target = tmp_path / "doc_es.txt"
    assert out == f"{target}\n"  # stdout carries only the paths, for scripts
    assert "HELLO WORLD" in target.read_text()
    assert "[1/1] doc.txt" in err and "1 translated of 1" in err


def test_translate_detects_each_document_language(langs, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text(ENGLISH_TEXT)
    status, out, err = run("translate", str(doc), "--to", "es")
    assert status == 0
    assert "detected English" in err
    assert out.strip() == str(tmp_path / "doc_es.txt")


def test_translate_takes_the_supported_documents_of_a_folder(langs, tmp_path):
    folder = tmp_path / "docs"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("one")
    (folder / "sub" / "b.txt").write_text("two")
    (folder / "notes.xyz").write_text("ignored")
    status, out, _ = run("translate", str(folder), "--from", "en", "--to", "es")
    assert status == 0
    assert out.split() == [str(folder / "a_es.txt"), str(folder / "sub" / "b_es.txt")]


def test_translate_into_an_output_folder_it_creates(langs, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello")
    target = tmp_path / "out" / "nested"
    status, out, _ = run(
        "translate", str(doc), "--from", "en", "--to", "es", "-o", str(target)
    )
    assert status == 0
    assert out.strip() == str(target / "doc_es.txt")


def test_translate_can_skip_documents_already_translated(langs, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello")
    (tmp_path / "doc_es.txt").write_text("already")
    status, out, err = run(
        "translate", str(doc), "--from", "en", "--to", "es", "--skip-existing"
    )
    assert status == 0 and out == ""
    assert "skipped: doc_es.txt already exists" in err
    assert (tmp_path / "doc_es.txt").read_text() == "already"


def test_a_failed_document_sets_the_exit_status(langs, tmp_path):
    good = tmp_path / "good.txt"
    good.write_text(ENGLISH_TEXT)
    empty = tmp_path / "empty.txt"
    empty.write_text("")  # nothing to detect a language from
    status, out, err = run("translate", str(empty), str(good), "--to", "es")
    assert status == cli.EXIT_FAILED
    assert "failed: empty.txt: could not detect the language" in err
    assert out.strip() == str(tmp_path / "good_es.txt")  # the rest still ran
    assert "1 translated, 1 failed of 2" in err


def test_quiet_prints_only_paths_and_failures(langs, tmp_path):
    good = tmp_path / "good.txt"
    good.write_text(ENGLISH_TEXT)
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    status, out, err = run("translate", str(empty), str(good), "--to", "es", "-q")
    assert status == cli.EXIT_FAILED
    assert out.strip() == str(tmp_path / "good_es.txt")
    assert err.strip() == "failed: empty.txt: could not detect the language"


def test_the_summary_reports_what_the_cache_answered(langs, tmp_path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("hello world")
    _, _, err = run(
        "translate", str(tmp_path / "a.txt"), str(tmp_path / "b.txt"),
        "--from", "en", "--to", "es",
    )
    assert "the cache answered 1 of 2 segments (50%)" in err


def test_the_cache_and_history_are_shared_with_the_window(langs, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    run("translate", str(doc), "--from", "en", "--to", "es")
    assert TranslationCache.db_info(TranslationCache.default_db_path())[0] == 1
    assert TranslationHistory.db_info(TranslationHistory.default_db_path())[0] == 1


def test_no_cache_and_no_history_leave_nothing_behind(langs, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    status, _, _ = run(
        "translate", str(doc), "--from", "en", "--to", "es", "--no-cache", "--no-history"
    )
    assert status == 0
    assert TranslationCache.db_info(TranslationCache.default_db_path())[0] == 0
    assert TranslationHistory.db_info(TranslationHistory.default_db_path())[0] == 0


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--to", "xx"], "no argos language with code 'xx' (installed: en, es)"),
        (["--from", "en", "--to", "en"], "the source and target languages are the same"),
        (["--from", "es", "--to", "en"], "no model Spanish → English"),
        (["--to", "es", "--threads", "0"], "--threads needs at least one thread"),
    ],
)
def test_requests_that_cannot_work_are_refused_up_front(langs, tmp_path, argv, message):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello")
    status, out, err = run("translate", str(doc), *argv)
    assert status == cli.EXIT_USAGE
    assert err.strip() == f"argonaut-cli: error: {message}"
    assert not (tmp_path / "doc_es.txt").exists()


def test_missing_and_unsupported_paths_are_refused(langs, tmp_path):
    status, _, err = run("translate", str(tmp_path / "nope.txt"), "--to", "es")
    assert status == cli.EXIT_USAGE and "no such file or folder" in err

    odd = tmp_path / "notes.xyz"
    odd.write_text("x")
    status, _, err = run("translate", str(odd), "--to", "es")
    assert status == cli.EXIT_USAGE and "unsupported format: notes.xyz" in err

    status, _, err = run("translate", str(tmp_path), "--to", "es")  # only notes.xyz
    assert status == cli.EXIT_USAGE and "no supported documents" in err


def test_without_installed_languages_it_says_how_to_get_one(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_languages", lambda *args, **kwargs: [])
    doc = tmp_path / "doc.txt"
    doc.write_text("hello")
    status, _, err = run("translate", str(doc), "--to", "es")
    assert status == cli.EXIT_USAGE and "packages install" in err


def test_a_pair_through_a_third_language_is_noted(langs, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "pivot_language", lambda translation: "French")
    doc = tmp_path / "doc.txt"
    doc.write_text("hello")
    _, _, err = run("translate", str(doc), "--from", "en", "--to", "es")
    assert "goes through French" in err


def test_the_nllb_engine_needs_its_model(monkeypatch, tmp_path):
    monkeypatch.setattr(nllb, "is_model_installed", lambda path=None: False)
    doc = tmp_path / "doc.txt"
    doc.write_text("hello")
    status, _, err = run("translate", str(doc), "--to", "es", "--engine", "nllb")
    assert status == cli.EXIT_USAGE and "argonaut-cli model download" in err


def test_load_languages_sets_up_argos_threads_and_beam(monkeypatch):
    monkeypatch.setattr(argostranslate.settings, "intra_threads", 0)
    monkeypatch.setattr(argostranslate.settings, "beam_size", 4)
    monkeypatch.setattr(
        cli.argostranslate.translate, "get_installed_languages", lambda: ["en"]
    )
    assert cli.load_languages("argos", threads=3, fast=True) == ["en"]
    assert argostranslate.settings.intra_threads == 3
    assert argostranslate.settings.beam_size == 1


def test_load_languages_hands_nllb_its_threads_and_beam(monkeypatch):
    calls = []
    monkeypatch.setattr(nllb, "is_model_installed", lambda path=None: True)
    monkeypatch.setattr(
        nllb, "get_installed_languages",
        lambda threads=None, beam_size=None: calls.append((threads, beam_size)) or [],
    )
    cli.load_languages("nllb", threads=2, fast=False)
    cli.load_languages("nllb", threads=2, fast=True)
    assert calls == [(2, nllb.NllbEngine.DEFAULT_BEAM), (2, 1)]


def test_the_first_ctrl_c_cancels_and_the_second_aborts():
    err = io.StringIO()
    interrupt = cli.Interrupt(err)
    interrupt(None, None)
    assert interrupt.is_set() and "Cancelling" in err.getvalue()
    with pytest.raises(KeyboardInterrupt):
        interrupt(None, None)


def test_a_cancelled_run_exits_like_an_interrupted_one(langs, tmp_path, monkeypatch):
    class AlreadyCancelled(cli.Interrupt):
        def __init__(self, stream):
            super().__init__(stream)
            self.requested = True  # as if Ctrl+C came before the first file

    doc = tmp_path / "doc.txt"
    doc.write_text("hello")
    monkeypatch.setattr(cli, "Interrupt", AlreadyCancelled)
    status, out, err = run("translate", str(doc), "--from", "en", "--to", "es")
    assert status == cli.EXIT_INTERRUPTED
    assert out == "" and "Cancelled: 0 translated of 1" in err


def test_live_progress_redraws_one_line_on_a_terminal():
    err = io.StringIO()
    report = cli.ConsoleReport(["/d/book.pdf"], out=io.StringIO(), err=err, live=True)
    report.on_progress(5, 10)
    report.on_phase("generating", {"name": "book.pdf", "done": 2, "total": 3})
    report.on_file_done(0, "/d/book_es.pdf", 3.0)
    text = err.getvalue()
    assert "\r  50% (5/10)" in text
    assert "\r  writing page 2/3" in text
    assert text.endswith("\r  translated in 00:03\n")  # the live line was cleared


# --- languages, packages and the model ---

def test_languages_lists_codes_and_names(langs):
    status, out, _ = run("languages")
    assert status == 0
    assert out.splitlines() == ["en   English", "es   Spanish"]


def test_languages_for_nllb_lists_its_codes_even_before_the_download(monkeypatch):
    monkeypatch.setattr(nllb, "is_model_installed", lambda path=None: False)
    status, out, err = run("languages", "--engine", "nllb")
    assert status == 0
    assert "es   Spanish" in out.splitlines()
    assert "model download" in err


def pkg(from_code, to_code, version="1.9"):
    names = {"en": "English", "es": "Spanish", "fr": "French", "de": "German"}
    return types.SimpleNamespace(
        type="translate", from_code=from_code, to_code=to_code,
        from_name=names[from_code], to_name=names[to_code],
        package_version=version, links=[],
    )


@pytest.fixture
def index(monkeypatch):
    """An index of three packages, en-es installed at its version, en-fr
    installed at an older one, en-de not installed."""
    available = [pkg("en", "de"), pkg("en", "es"), pkg("en", "fr")]
    installed = {("en", "es"): "1.9", ("en", "fr"): "1.0"}
    actions = []
    monkeypatch.setattr(packages, "get_available", lambda: available)
    monkeypatch.setattr(packages, "installed_versions", lambda: dict(installed))
    monkeypatch.setattr(
        packages, "install",
        lambda p, on_progress=None, is_cancelled=None: actions.append(
            ("install", packages.pair(p))
        ),
    )
    monkeypatch.setattr(packages, "uninstall", lambda pair: actions.append(("remove", pair)))
    return actions


def test_packages_list_shows_each_state(index):
    status, out, _ = run("packages", "list")
    assert status == 0
    lines = out.splitlines()
    assert lines[0].startswith("en-de") and lines[0].endswith("not installed")
    assert lines[1].endswith("installed") and "English → Spanish" in lines[1]
    assert lines[2].endswith("update available (1.0 → 1.9)")

    _, out, _ = run("packages", "list", "--installed")
    assert [line[:5] for line in out.splitlines()] == ["en-es", "en-fr"]
    _, out, _ = run("packages", "list", "--updates")
    assert [line[:5] for line in out.splitlines()] == ["en-fr"]


def test_packages_install_installs_only_what_is_missing(index):
    status, _, err = run("packages", "install", "en-de", "en-es")
    assert status == 0
    assert index == [("install", ("en", "de"))]
    assert "English → Spanish is already installed" in err


def test_packages_install_refuses_pairs_the_index_lacks(index):
    status, _, err = run("packages", "install", "en-xx")
    assert status == cli.EXIT_USAGE and "no package for en-xx" in err
    status, _, err = run("packages", "install", "enes")
    assert status == cli.EXIT_USAGE and "FROM-TO" in err
    assert index == []


def test_a_failed_install_sets_the_exit_status(index, monkeypatch):
    def broken(p, on_progress=None, is_cancelled=None):
        raise RuntimeError("offline")

    monkeypatch.setattr(packages, "install", broken)
    status, _, err = run("packages", "install", "en-de")
    assert status == cli.EXIT_FAILED and "failed: offline" in err


def test_packages_update_reinstalls_the_outdated_ones(index):
    status, _, _ = run("packages", "update")
    assert status == 0 and index == [("install", ("en", "fr"))]


def test_packages_update_of_a_current_pair_has_nothing_to_do(index):
    status, _, err = run("packages", "update", "en-es")
    assert status == 0 and "up to date" in err and index == []
    status, _, err = run("packages", "update", "en-de")
    assert status == cli.EXIT_USAGE and "en-de is not installed" in err


def test_packages_remove_uninstalls_installed_pairs_only(index):
    status, _, err = run("packages", "remove", "en-de")
    assert status == cli.EXIT_USAGE and "en-de is not installed" in err
    status, _, _ = run("packages", "remove", "en-es", "en-fr")
    assert status == 0
    assert index == [("remove", ("en", "es")), ("remove", ("en", "fr"))]


def test_an_unreachable_index_is_reported(monkeypatch):
    def offline():
        raise RuntimeError("no network")

    monkeypatch.setattr(packages, "get_available", offline)
    monkeypatch.setattr(packages, "installed_versions", lambda: {})
    status, _, err = run("packages", "list")
    assert status == cli.EXIT_USAGE
    assert "could not fetch the package index: no network" in err


def test_model_status_download_and_remove(monkeypatch, tmp_path):
    state = {"installed": False}
    model = tmp_path / "model"
    monkeypatch.setattr(nllb, "is_model_installed", lambda path=None: state["installed"])
    monkeypatch.setattr(nllb, "model_dir", lambda: str(model))

    def download(path=None, base_url=None, on_progress=None, is_cancelled=None):
        model.mkdir()
        (model / "model.bin").write_bytes(b"x" * 2048)
        state["installed"] = True

    def remove(path=None):
        state["installed"] = False

    monkeypatch.setattr(nllb, "download_model", download)
    monkeypatch.setattr(nllb, "remove_model", remove)

    _, out, _ = run("model", "status")
    assert out.startswith("not installed")
    assert run("model", "download")[0] == 0
    _, out, _ = run("model", "status")
    assert out.strip() == f"installed in {model} (2.0 KB)"
    _, _, err = run("model", "download")
    assert "already installed" in err
    assert run("model", "remove")[0] == 0 and not state["installed"]
    _, _, err = run("model", "remove")
    assert "not installed" in err


def test_a_cancelled_model_download_exits_like_an_interrupted_one(monkeypatch):
    def cancelled(path=None, base_url=None, on_progress=None, is_cancelled=None):
        raise CancelledError()

    monkeypatch.setattr(nllb, "is_model_installed", lambda path=None: False)
    monkeypatch.setattr(nllb, "download_model", cancelled)
    status, _, err = run("model", "download")
    assert status == cli.EXIT_INTERRUPTED and "Cancelled" in err


# --- history and cache ---

def test_history_lists_what_was_translated_newest_first(langs, tmp_path):
    assert run("history", "list") == (0, "", "")  # nothing recorded yet
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("hello world")
        run("translate", str(tmp_path / name), "--from", "en", "--to", "es")
    status, out, _ = run("history", "list")
    lines = out.splitlines()
    assert status == 0 and len(lines) == 2
    assert "en→es" in lines[0] and f"{tmp_path / 'b.txt'} → {tmp_path / 'b_es.txt'}" in lines[0]
    assert "(cache 100%)" in lines[0]  # b.txt repeated a.txt word for word
    _, out, _ = run("history", "list", "-n", "1")
    assert len(out.splitlines()) == 1


def test_clearing_needs_yes_when_there_is_no_one_to_ask(langs, tmp_path, monkeypatch):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    run("translate", str(doc), "--from", "en", "--to", "es")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))  # not a terminal

    for command in ("history", "cache"):
        status, _, err = run(command, "clear")
        assert status == cli.EXIT_USAGE and "--yes" in err
    assert TranslationHistory.db_info(TranslationHistory.default_db_path())[0] == 1

    status, _, err = run("history", "clear", "--yes")
    assert status == 0 and "Deleted 1 entries" in err
    status, _, err = run("cache", "clear", "-y")
    assert status == 0 and "Deleted 1 segments" in err
    _, _, err = run("history", "clear")
    assert "empty" in err


def test_clearing_asks_on_a_terminal(langs, tmp_path, monkeypatch):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    run("translate", str(doc), "--from", "en", "--to", "es")

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", Terminal("n\n"))
    run("cache", "clear")
    assert TranslationCache.db_info(TranslationCache.default_db_path())[0] == 1
    monkeypatch.setattr(cli.sys, "stdin", Terminal("y\n"))
    _, _, err = run("cache", "clear")
    assert "Delete the 1 cached segments? [y/N]" in err
    assert TranslationCache.db_info(TranslationCache.default_db_path())[0] == 0


def test_cache_info_reports_its_size(langs, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    run("translate", str(doc), "--from", "en", "--to", "es")
    status, out, _ = run("cache", "info")
    assert status == 0 and out.startswith("1 segments, ")


# --- odds and ends ---

def test_version(capsys):
    from argonaut import __version__

    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"argonaut-cli {__version__}"


def test_a_second_ctrl_c_aborts_with_the_interrupted_status(monkeypatch):
    def abort(*args, **kwargs):
        raise KeyboardInterrupt  # what Interrupt raises on the second Ctrl+C

    monkeypatch.setattr(cli, "cmd_languages", abort)
    status, _, err = run("languages")
    assert status == cli.EXIT_INTERRUPTED and "Aborted" in err


@pytest.mark.parametrize(
    "seconds, text", [(5, "00:05"), (65, "01:05"), (3600 + 120, "1h 02m")]
)
def test_format_duration(seconds, text):
    assert cli.format_duration(seconds) == text


@pytest.mark.parametrize(
    "size, text", [(0, "0 B"), (1023, "1023 B"), (2048, "2.0 KB"), (3 * 2**30, "3.0 GB")]
)
def test_human_size(size, text):
    assert cli.human_size(size) == text
