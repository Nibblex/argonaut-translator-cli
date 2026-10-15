"""The batch on its own, with no interface attached: what it translates,
skips and records, and what it reports to its listener."""

import pytest

from argonaut_cli import nllb, packages
from argonaut_cli.batch import (
    AlreadyInLanguage,
    Batch,
    LanguageNotDetected,
    Listener,
    NoModel,
    documents_in,
)
from argonaut_cli.history import TranslationHistory
from argonaut_cli.translation import CancelledError, TranslationCache
from tests.conftest import FakeBatchTranslation, FakeLanguage, FakeTranslation
from tests.test_pdf import make_pdf
from tests.test_translation import ENGLISH_TEXT


class Recorder(Listener):
    """Keeps every report the batch makes."""

    def __init__(self):
        self.started, self.detected, self.progress, self.phases = [], [], [], []
        self.done, self.failed, self.skipped, self.stats = [], [], [], []

    def on_file_started(self, index, path):
        self.started.append(index)

    def on_language_detected(self, index, lang):
        self.detected.append((index, lang))

    def on_progress(self, done, total):
        self.progress.append((done, total))

    def on_phase(self, key, kwargs):
        self.phases.append(key)

    def on_file_done(self, index, out_path, seconds):
        self.done.append((out_path, seconds))

    def on_file_failed(self, index, error):
        self.failed.append(error)

    def on_file_skipped(self, index, out_path):
        self.skipped.append(out_path)

    def on_cache_stats(self, index, file_reused, batch_reused, batch_segments):
        self.stats.append((index, file_reused, batch_reused, batch_segments))


def make_langs():
    english = FakeLanguage("en", "English")
    spanish = FakeLanguage("es", "Spanish")
    english._translations["es"] = FakeTranslation(english, spanish)
    return english, spanish


def make_batch(files, src, dst, languages=(), **kwargs):
    recorder = Recorder()
    batch = Batch(src, dst, list(languages), files, listener=recorder, **kwargs)
    return batch, recorder


def persistent():
    """The keyword arguments of a batch that keeps its cache and history
    on disk, as the window's default settings do."""
    return {
        "cache_db_path": TranslationCache.default_db_path(),
        "history_db_path": TranslationHistory.default_db_path(),
    }


def test_output_path_next_to_original():
    _, spanish = make_langs()
    batch, _ = make_batch([], None, spanish)
    translation = FakeTranslation(to_lang=spanish)
    assert batch.get_output_path(translation, "/data/report.txt") == "/data/report_es.txt"


def test_output_path_in_chosen_folder():
    _, spanish = make_langs()
    batch, _ = make_batch([], None, spanish, output_dir="/out")
    translation = FakeTranslation(to_lang=spanish)
    assert batch.get_output_path(translation, "/data/report.txt") == "/out/report_es.txt"


def test_resolve_translation_rejects_same_language():
    english, _ = make_langs()
    batch, _ = make_batch([], english, english)
    with pytest.raises(AlreadyInLanguage) as caught:
        batch.resolve_translation(0, "/data/report.txt")
    assert caught.value.name == "report.txt" and caught.value.lang is english
    assert str(caught.value) == "report.txt: the document is already in English"


def test_resolve_translation_requires_a_model():
    english, spanish = make_langs()
    batch, _ = make_batch([], spanish, english)  # no es->en model registered
    with pytest.raises(NoModel) as caught:
        batch.resolve_translation(0, "/data/report.txt")
    assert (caught.value.src, caught.value.dst) == (spanish, english)
    assert str(caught.value) == "report.txt: no model Spanish → English"


def test_resolve_translation_detects_language(tmp_path):
    english, spanish = make_langs()
    doc = tmp_path / "doc.txt"
    doc.write_text(ENGLISH_TEXT)
    batch, recorder = make_batch([str(doc)], None, spanish, languages=[english, spanish])
    translation = batch.resolve_translation(0, str(doc))
    assert translation is english._translations["es"]
    assert recorder.detected == [(0, english)]  # the Language: naming it is the UI's job


def test_run_translates_files(tmp_path):
    english, spanish = make_langs()
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    batch, recorder = make_batch([str(doc)], english, spanish)
    batch.run()

    assert recorder.failed == []
    out = tmp_path / "doc_es.txt"
    assert [path for path, _ in recorder.done] == [str(out)]
    assert "HELLO WORLD" in out.read_text()
    assert recorder.done[0][1] >= 0


def test_run_translates_pdfs_reporting_the_page_phases(tmp_path):
    """PDFs take the FastPdfTranslator path: the file still lands in
    on_file_done, and the listener is told the page each phase is on."""
    english = FakeLanguage("en", "English")
    spanish = FakeLanguage("es", "Spanish")
    english._translations["es"] = FakeBatchTranslation(english, spanish)
    doc = tmp_path / "doc.pdf"
    make_pdf(doc)
    batch, recorder = make_batch([str(doc)], english, spanish)
    batch.run()

    assert recorder.failed == []
    out = tmp_path / "doc_es.pdf"
    assert [path for path, _ in recorder.done] == [str(out)] and out.exists()
    # every long phase reported, in order, with the save last
    assert recorder.phases[0] == "reading"
    assert "translating_page" in recorder.phases and "generating" in recorder.phases
    assert recorder.phases[-1] == "saving"
    assert (0, 2) in recorder.progress  # the two paragraphs, counted while parsing


def test_batch_shares_the_translation_cache_across_files(tmp_path):
    # two files with the same content translate their shared paragraph once
    english, spanish = make_langs()
    inner = english._translations["es"]
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("hello world")
    batch, recorder = make_batch(
        [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")], english, spanish
    )
    batch.run()

    assert len(recorder.done) == 2
    assert inner.calls == 1  # the second file reused the first's translation
    # first file: nothing to reuse yet; second: its one segment came from cache.
    # The last figure is what the batch reuse is out of: one segment per file
    assert recorder.stats == [(0, 0, 0, 1), (1, 1, 1, 2)]


def test_cache_entries_do_not_survive_a_package_upgrade(tmp_path, monkeypatch):
    """The persistent cache is namespaced by the installed package version:
    after an upgrade the old model's translations must not be served."""
    english, spanish = make_langs()
    inner = english._translations["es"]
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    versions = {("en", "es"): "1.0"}
    monkeypatch.setattr(packages, "installed_versions", lambda: dict(versions))

    make_batch([str(doc)], english, spanish, **persistent())[0].run()
    assert inner.calls == 1
    make_batch([str(doc)], english, spanish, **persistent())[0].run()
    assert inner.calls == 1  # same version: served from the persistent cache

    versions[("en", "es")] = "1.9"  # the pair was upgraded
    make_batch([str(doc)], english, spanish, **persistent())[0].run()
    assert inner.calls == 2  # the old model's entry is no longer valid


def test_an_in_memory_cache_does_not_outlive_its_batch(tmp_path):
    english, spanish = make_langs()
    inner = english._translations["es"]
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    make_batch([str(doc)], english, spanish)[0].run()
    make_batch([str(doc)], english, spanish)[0].run()
    assert inner.calls == 2  # no cache_db_path: nothing was kept between runs


def test_engine_version_tags_argos_pairs_and_the_nllb_model(monkeypatch):
    english, spanish = make_langs()
    translation = english._translations["es"]
    monkeypatch.setattr(
        packages, "installed_versions", lambda: {("en", "es"): "1.7"}
    )
    batch, _ = make_batch([], english, spanish)
    assert batch.engine_version(translation) == "1.7"
    assert batch.engine_version(translation) == "1.7"  # read once, reused

    nllb_batch, _ = make_batch([], english, spanish, engine_id="nllb")
    assert nllb_batch.engine_version(translation) == nllb.MODEL_REPO


def test_greedy_decoding_namespaces_the_cache_apart(monkeypatch):
    """Greedy output differs from beam output, so fast mode must never be
    served quality-mode cache entries or poison them with its own."""
    english, spanish = make_langs()
    translation = english._translations["es"]
    monkeypatch.setattr(
        packages, "installed_versions", lambda: {("en", "es"): "1.7"}
    )
    batch, _ = make_batch([], english, spanish, greedy=True)
    assert batch.engine_version(translation) == "1.7+greedy"
    nllb_batch, _ = make_batch([], english, spanish, engine_id="nllb", greedy=True)
    assert nllb_batch.engine_version(translation) == nllb.MODEL_REPO + "+greedy"


def test_a_finished_batch_is_recorded_in_the_history(tmp_path):
    english, spanish = make_langs()
    source = tmp_path / "note.txt"
    source.write_text("hello world")
    make_batch([str(source)], english, spanish, **persistent())[0].run()

    history = TranslationHistory(TranslationHistory.default_db_path(), ttl_days=0)
    entry, = history.entries()
    history.close()
    assert entry.source_path == str(source)
    assert entry.output_path == str(tmp_path / "note_es.txt")
    assert (entry.from_code, entry.to_code) == ("en", "es")
    assert entry.engine == "argos"
    assert entry.seconds >= 0


def test_each_history_row_carries_that_file_cache_figures(tmp_path):
    """The dialog's cache column reads these, so each row has to carry its own
    file's share rather than the batch's running total."""
    english, spanish = make_langs()
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("hello world")
    make_batch(
        [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")], english, spanish,
        **persistent(),
    )[0].run()

    history = TranslationHistory(TranslationHistory.default_db_path(), ttl_days=0)
    rows = {entry.source_path: entry for entry in history.entries()}
    history.close()
    first = rows[str(tmp_path / "a.txt")]
    second = rows[str(tmp_path / "b.txt")]
    assert (first.reused, first.segments) == (0, 1)   # nothing to reuse yet
    assert (second.reused, second.segments) == (1, 1)  # served from the cache


def test_a_failed_file_is_not_recorded(tmp_path):
    """The history lists translations, not attempts."""
    english, spanish = make_langs()
    make_batch([str(tmp_path / "missing.txt")], english, spanish, **persistent())[0].run()

    assert TranslationHistory.db_info(TranslationHistory.default_db_path())[0] == 0


def test_without_a_history_path_nothing_is_recorded(tmp_path):
    english, spanish = make_langs()
    source = tmp_path / "note.txt"
    source.write_text("hello world")
    batch, _ = make_batch([str(source)], english, spanish)
    batch.run()

    assert not batch.history.enabled
    assert TranslationHistory.db_info(TranslationHistory.default_db_path())[0] == 0
    assert (tmp_path / "note_es.txt").exists()  # the translation still happened


def test_run_reports_failures_and_continues(tmp_path):
    english, spanish = make_langs()
    good = tmp_path / "good.txt"
    good.write_text("hello world")
    missing = tmp_path / "missing.txt"
    batch, recorder = make_batch([str(missing), str(good)], english, spanish)
    batch.run()

    assert len(recorder.failed) == 1
    assert [path for path, _ in recorder.done] == [str(tmp_path / "good_es.txt")]


def test_run_skips_existing_output(tmp_path):
    english, spanish = make_langs()
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    existing = tmp_path / "doc_es.txt"
    existing.write_text("already translated")
    batch, recorder = make_batch([str(doc)], english, spanish, skip_existing=True)
    batch.run()

    assert recorder.skipped == [str(existing)]
    assert recorder.done == []
    assert existing.read_text() == "already translated"  # left untouched


def test_run_overwrites_when_skip_disabled(tmp_path):
    english, spanish = make_langs()
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    out = tmp_path / "doc_es.txt"
    out.write_text("stale")
    batch, recorder = make_batch([str(doc)], english, spanish)  # skip off by default
    batch.run()

    assert recorder.skipped == []
    assert [path for path, _ in recorder.done] == [str(out)]
    assert "HELLO WORLD" in out.read_text()


def test_skip_happens_before_language_detection(tmp_path):
    # an existing output is skipped without reading the file, so a document
    # that detection could not handle still counts as skipped, not failed
    _, spanish = make_langs()
    doc = tmp_path / "doc.txt"
    doc.write_text("")  # empty: language detection would fail
    (tmp_path / "doc_es.txt").write_text("x")
    batch, recorder = make_batch(
        [str(doc)], None, spanish, languages=[spanish], skip_existing=True
    )
    batch.run()

    assert recorder.skipped == [str(tmp_path / "doc_es.txt")]
    assert recorder.failed == []


def test_run_fails_a_file_whose_language_cannot_be_detected(tmp_path):
    _, spanish = make_langs()
    doc = tmp_path / "doc.txt"
    doc.write_text("")  # nothing to detect a language from
    batch, recorder = make_batch([str(doc)], None, spanish, languages=[spanish])
    batch.run()

    error, = recorder.failed
    assert isinstance(error, LanguageNotDetected) and error.path == str(doc)
    assert str(error) == "doc.txt: could not detect the language"


def test_cancelling_mid_file_stops_the_batch(tmp_path):
    """A cancel that lands while a file is being translated ends the run:
    the interrupted file gets no outcome and the next one never starts."""
    english, spanish = make_langs()

    def translate(text):
        raise CancelledError()  # what the proxy raises once cancel arrives

    english._translations["es"].translate = translate
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("hello world")
    batch, recorder = make_batch(
        [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")], english, spanish
    )
    batch.run()

    assert recorder.started == [0]  # the second file never started
    assert recorder.done == [] and recorder.failed == []  # cancelling is not an error


def test_cancelled_run_stops_before_translating(tmp_path):
    english, spanish = make_langs()
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    batch, recorder = make_batch([str(doc)], english, spanish, is_cancelled=lambda: True)
    batch.run()

    assert recorder.started == [] and recorder.done == []


def test_a_batch_runs_without_a_listener(tmp_path):
    english, spanish = make_langs()
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")
    Batch(english, spanish, [], [str(doc)]).run()
    assert "HELLO WORLD" in (tmp_path / "doc_es.txt").read_text()


def test_documents_in_walks_folders_and_keeps_supported_files(tmp_path):
    folder = tmp_path / "docs"
    (folder / "sub").mkdir(parents=True)
    for name in ("b.txt", "a.TXT", "notes.xyz", "sub/c.html"):
        (folder / name).write_text("x")
    loose = tmp_path / "loose.txt"
    loose.write_text("x")

    found = list(documents_in([str(loose), str(folder), str(folder / "b.txt")]))

    assert found == [
        str(loose),
        str(folder / "a.TXT"),  # extensions match whatever their case
        str(folder / "b.txt"),
        str(folder / "sub" / "c.html"),  # subfolders after the folder's own files
    ]  # notes.xyz is unsupported, and b.txt named twice is listed once
