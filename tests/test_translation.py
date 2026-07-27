import sqlite3

import pytest

from argonaut.translation import (
    LANGDETECT_TO_ARGOS,
    SUPPORTED_EXTS,
    CancelledError,
    ProgressTranslation,
    TranslationCache,
    detect_language,
)
from tests.conftest import FakeBatchTranslation, FakeLanguage, FakeTranslation

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


def test_shared_cache_reuses_translations_across_proxies():
    # a cache passed to several proxies stands in for a whole batch: the same
    # text translates once no matter how many files (proxies) contain it
    cache = TranslationCache()
    en, es = FakeLanguage("en", "English"), FakeLanguage("es", "Spanish")
    inner = FakeTranslation(en, es)
    first = ProgressTranslation(inner, [].append, lambda: False, cache=cache)
    second = ProgressTranslation(inner, [].append, lambda: False, cache=cache)

    assert first.translate("hello") == "HELLO"
    assert second.translate("hello") == "HELLO"  # served from the shared cache
    assert inner.calls == 1
    assert cache.reused == 1  # the second proxy's hit is counted for the batch
    assert second.reused == 1 and first.reused == 0  # per-file breakdown


def test_the_proxy_counts_the_segments_its_reuse_is_out_of():
    """The reuse rate the summary reports needs a denominator, and chunks the
    cache never sees (numbers, punctuation) are not misses."""
    inner, progress, proxy = make_proxy()
    proxy.translate_many(["hello", "42", "—"])
    assert (proxy.segments, proxy.reused) == (1, 0)  # only "hello" is cacheable

    # repeats within one call are deduplicated rather than counted as reuse,
    # so the hit comes from what an earlier call stored
    proxy.translate_many(["hello", "world"])
    assert (proxy.segments, proxy.reused) == (3, 1)
    assert inner.calls == 2


def test_shared_cache_keeps_language_pairs_apart():
    # the same text detected in different source languages must not collide:
    # each pair keeps its own entry even in one shared cache
    cache = TranslationCache()
    es, en = FakeLanguage("es", "Spanish"), FakeLanguage("en", "English")
    it = FakeLanguage("it", "Italian")
    es_en = ProgressTranslation(
        FakeTranslation(es, en), [].append, lambda: False, cache=cache
    )
    it_en = ProgressTranslation(
        FakeTranslation(it, en), [].append, lambda: False, cache=cache
    )

    es_en.translate("ciao")
    it_en.translate("ciao")
    assert cache.hits == 0 and cache.misses == 2  # both were first sightings


def test_shared_cache_keeps_model_versions_apart():
    # the same pair translated by two versions of a model must not share
    # entries: the old model's output would otherwise outlive the upgrade
    cache = TranslationCache()
    en, es = FakeLanguage("en", "English"), FakeLanguage("es", "Spanish")
    old = ProgressTranslation(
        FakeTranslation(en, es), [].append, lambda: False,
        cache=cache, engine_id="argos", engine_version="1.0",
    )
    new = ProgressTranslation(
        FakeTranslation(en, es), [].append, lambda: False,
        cache=cache, engine_id="argos", engine_version="1.9",
    )

    old.translate("hello")
    new.translate("hello")
    assert cache.hits == 0 and cache.misses == 2  # no cross-version reuse


def test_translation_cache_counts_hits_and_misses():
    cache = TranslationCache()
    assert cache.lookup("k") == (None, False)  # miss on an empty cache
    cache.store("k", "v")
    assert cache.lookup("k") == ("v", True)  # now a hit
    assert (cache.hits, cache.misses, cache.reused) == (1, 1, 1)


def test_closing_a_cache_releases_the_database_but_keeps_the_counters(tmp_path):
    """A batch holds every segment it translated; runs that never let go of
    them (a cancelled one included) would keep whole books in memory."""
    db = str(tmp_path / "cache.db")
    cache = TranslationCache(db, ttl_days=0)
    cache.store("k", "v")
    assert cache.lookup("k") == ("v", True)

    cache.close()
    assert cache.reused == 1  # the window still reports what the run reused
    assert cache.lookup("k") == (None, False)  # released, but still usable

    reopened = TranslationCache(db, ttl_days=0)
    assert reopened.lookup("k") == ("v", True)  # what it stored survives
    reopened.close()


def test_batching_engine_receives_only_the_misses_in_one_call():
    """An engine with translate_many (NLLB) gets the cache misses batched
    into a single call instead of one call per text."""
    inner = FakeBatchTranslation()
    progress = []
    proxy = ProgressTranslation(inner, progress.append, lambda: False)
    proxy.translate("hello")  # cached from now on

    assert proxy.translate_many(["hello", "world", "again"]) == [
        "HELLO", "WORLD", "AGAIN"
    ]
    assert inner.batches == [["hello"], ["world", "again"]]
    assert progress == [1, 4]


def test_repeated_texts_in_one_call_reach_the_engine_once():
    """A text repeated within a single call (headers, footers) misses the
    cache at every position, but the engine translates it only once."""
    inner = FakeBatchTranslation()
    proxy = ProgressTranslation(inner, [].append, lambda: False)

    assert proxy.translate_many(["same", "other", "same"]) == [
        "SAME", "OTHER", "SAME"
    ]
    assert inner.batches == [["same", "other"]]


def test_database_writes_are_committed_in_groups(tmp_path):
    """Segments are committed a group at a time — a commit per segment costs
    a WAL write each, and a crash only loses the last, retranslatable, group."""
    db = str(tmp_path / "cache.db")
    cache = TranslationCache(db)
    for n in range(TranslationCache.COMMIT_EVERY):
        cache.store(f"k{n}", "v")

    other = sqlite3.connect(db)  # a write still waiting would be invisible
    count = other.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    other.close()
    cache.close()
    assert count == TranslationCache.COMMIT_EVERY


def test_purge_db_deletes_every_entry_and_reports_the_count(tmp_path):
    db = str(tmp_path / "cache.db")
    cache = TranslationCache(db)
    cache.store("k1", "v1")
    cache.store("k2", "v2")
    cache.close()

    assert TranslationCache.purge_db(db) == 2
    assert TranslationCache.db_info(db)[0] == 0
    reopened = TranslationCache(db)
    assert reopened.lookup("k1") == (None, False)
    reopened.close()


def test_unopenable_db_falls_back_to_memory(tmp_path):
    in_the_way = tmp_path / "not-a-dir"
    in_the_way.write_text("")  # a file where the DB's directory should go
    cache = TranslationCache(str(in_the_way / "cache.db"))
    cache.store("k", "v")
    assert cache.lookup("k") == ("v", True)  # in-memory keeps working
    cache.close()


def test_open_migrates_a_cache_without_timestamps(tmp_path):
    """A DB created before the TTL column must gain it on open, with its
    rows stamped as fresh so they don't all expire on the first prune."""
    db = str(tmp_path / "cache.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE cache (key_hash TEXT PRIMARY KEY, translation TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO cache VALUES (?, ?)", (TranslationCache._hash("k"), "v")
    )
    conn.commit()
    conn.close()

    cache = TranslationCache(db, ttl_days=30)  # the prune runs on open
    assert cache.lookup("k") == ("v", True)  # migrated rows survive it
    cache.close()


def test_text_without_letters_is_passed_through():
    inner, _, proxy = make_proxy()
    assert proxy.translate("12 / 34") == "12 / 34"
    assert inner.calls == 0


def test_cancellation_raises():
    _, _, proxy = make_proxy(cancelled=True)
    with pytest.raises(CancelledError):
        proxy.translate("hello")


def test_cancelling_stops_between_texts_of_a_batch():
    """Engines without translate_many are called text by text, so cancelling
    must not wait for the rest of the batch (a whole PDF page)."""
    inner = FakeTranslation()
    assert not hasattr(inner, "translate_many")
    cancelled = []
    proxy = ProgressTranslation(
        inner, lambda done: None, lambda: bool(cancelled)
    )
    original = inner.translate

    def translate(text):
        cancelled.append(True)  # cancel arrives while the first text is in flight
        return original(text)

    inner.translate = translate
    with pytest.raises(CancelledError):
        proxy.translate_many(["one", "two", "three"])
    assert inner.calls == 1  # the other two were never translated


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


def test_detect_language_returns_none_for_featureless_text(tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("1234567890 !!!")  # content, but nothing to detect from
    assert detect_language(str(doc), [FakeLanguage("en", "English")]) is None


def test_purge_db_reports_zero_when_the_db_is_unusable(tmp_path):
    broken = tmp_path / "not-a-db"
    broken.write_text("plain text, not sqlite")
    assert TranslationCache.purge_db(str(broken)) == 0


def test_entries_holding_the_unknown_marker_are_retranslated(tmp_path):
    """NLLB used to answer typographic punctuation with its unknown token,
    which decodes to "⁇". Caches filled then still hold the damaged text,
    and serving it would outlive the engine fix."""
    from argonaut.translation import UNKNOWN_MARKER

    db = str(tmp_path / "cache.db")
    cache = TranslationCache(db)
    cache.store("k", f"de {UNKNOWN_MARKER} riot {UNKNOWN_MARKER}")
    cache.close()

    reopened = TranslationCache(db)
    assert reopened.lookup("k") == (None, False)  # reported as a miss
    assert reopened.misses == 1 and reopened.hits == 0

    reopened.store("k", 'de "riot"')  # the fresh translation replaces it
    assert reopened.lookup("k") == ('de "riot"', True)
    reopened.close()

    healed = TranslationCache(db)
    assert healed.lookup("k") == ('de "riot"', True)  # the repair persisted
    healed.close()


def test_a_poisoned_memory_entry_is_dropped(tmp_path):
    """The same check applies to the in-memory level, which a batch fills
    from the database on the first lookup."""
    from argonaut.translation import UNKNOWN_MARKER

    cache = TranslationCache()
    cache.store("k", f"algo {UNKNOWN_MARKER} roto")
    assert cache.lookup("k") == (None, False)
    assert cache.lookup("k") == (None, False)  # dropped, not merely skipped
    assert cache.hits == 0


def test_a_translation_with_a_real_question_mark_is_kept():
    """Only the double-question-mark marker disqualifies an entry; ordinary
    punctuation must not send perfectly good translations back to the engine."""
    cache = TranslationCache()
    cache.store("k", "¿Qué pasó? Nadie lo sabe.")
    assert cache.lookup("k") == ("¿Qué pasó? Nadie lo sabe.", True)
    assert cache.hits == 1
