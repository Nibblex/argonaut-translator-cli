import os
import sqlite3
import time

from argonaut.history import TranslationHistory


def make_history(tmp_path, ttl_days=0):
    return TranslationHistory(str(tmp_path / "history.db"), ttl_days=ttl_days)


def record_one(history, name="report.docx", when=None, seconds=1.5):
    history.record(
        f"/docs/{name}", f"/out/{name}", "en", "es", "argos", seconds, when=when
    )


def test_records_and_reads_back_a_translation(tmp_path):
    history = make_history(tmp_path)
    record_one(history)
    entry, = history.entries()
    history.close()

    assert entry.source_path == "/docs/report.docx"
    assert entry.output_path == "/out/report.docx"
    assert (entry.from_code, entry.to_code) == ("en", "es")
    assert entry.engine == "argos"
    assert entry.seconds == 1.5
    assert entry.translated_at > 0


def test_entries_come_back_newest_first(tmp_path):
    history = make_history(tmp_path)
    now = int(time.time())
    record_one(history, "old.txt", when=now - 100)
    record_one(history, "new.txt", when=now)
    names = [os.path.basename(e.source_path) for e in history.entries()]
    history.close()
    assert names == ["new.txt", "old.txt"]


def test_entries_honour_a_limit(tmp_path):
    history = make_history(tmp_path)
    now = int(time.time())
    for i in range(5):
        record_one(history, f"f{i}.txt", when=now + i)
    assert len(history.entries()) == 5
    assert [os.path.basename(e.source_path) for e in history.entries(limit=2)] == [
        "f4.txt", "f3.txt"
    ]
    history.close()


def test_entries_recorded_in_the_same_second_keep_their_order(tmp_path):
    """Two files from one batch can share a timestamp; the insertion order
    breaks the tie so the list never shuffles between openings."""
    history = make_history(tmp_path)
    now = int(time.time())
    record_one(history, "first.txt", when=now)
    record_one(history, "second.txt", when=now)
    names = [os.path.basename(e.source_path) for e in history.entries()]
    history.close()
    assert names == ["second.txt", "first.txt"]


def test_history_survives_reopening(tmp_path):
    history = make_history(tmp_path)
    record_one(history)
    history.close()

    reopened = make_history(tmp_path)
    assert len(reopened.entries()) == 1
    reopened.close()


def test_expired_entries_are_pruned_on_open(tmp_path):
    history = make_history(tmp_path)
    now = int(time.time())
    record_one(history, "ancient.txt", when=now - 40 * 86400)
    record_one(history, "recent.txt", when=now - 2 * 86400)
    history.close()

    pruned = make_history(tmp_path, ttl_days=30)
    names = [os.path.basename(e.source_path) for e in pruned.entries()]
    pruned.close()
    assert names == ["recent.txt"]


def test_a_ttl_of_zero_keeps_everything(tmp_path):
    history = make_history(tmp_path)
    record_one(history, "ancient.txt", when=int(time.time()) - 3650 * 86400)
    history.close()

    kept = make_history(tmp_path, ttl_days=0)
    assert len(kept.entries()) == 1
    kept.close()


def test_the_cache_figures_of_a_translation_are_recorded(tmp_path):
    history = make_history(tmp_path)
    history.record(
        "/docs/a.txt", "/out/a.txt", "en", "es", "argos", 1.0,
        reused=30, segments=120,
    )
    entry, = history.entries()
    history.close()
    assert (entry.reused, entry.segments) == (30, 120)


def test_a_translation_that_reported_no_figures_reads_as_zero(tmp_path):
    history = make_history(tmp_path)
    record_one(history)  # the worker's own call passes them, this one does not
    entry, = history.entries()
    history.close()
    assert (entry.reused, entry.segments) == (0, 0)


def test_a_history_from_an_older_version_gains_the_cache_columns(tmp_path):
    """The table is created with IF NOT EXISTS, so an existing history keeps
    the schema it was written with: the columns added since have to be added
    to it, or every read would fail and the history would look lost."""
    db = str(tmp_path / "history.db")
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, translated_at INTEGER NOT NULL, "
        "source_path TEXT NOT NULL, output_path TEXT NOT NULL, "
        "from_code TEXT NOT NULL, to_code TEXT NOT NULL, engine TEXT NOT NULL, "
        "seconds REAL NOT NULL)"
    )
    old.execute(
        "INSERT INTO history (translated_at, source_path, output_path, "
        "from_code, to_code, engine, seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()) - 60, "/docs/old.txt", "/out/old.txt",
         "en", "es", "argos", 2.0),
    )
    old.commit()
    old.close()

    history = TranslationHistory(db, ttl_days=0)
    history.record(
        "/docs/new.txt", "/out/new.txt", "en", "es", "nllb", 1.0,
        reused=5, segments=10,
    )
    entries = history.entries()
    history.close()

    assert [os.path.basename(e.source_path) for e in entries] == [
        "new.txt", "old.txt"  # the old row is still there, and still readable
    ]
    assert (entries[0].reused, entries[0].segments) == (5, 10)
    assert (entries[1].reused, entries[1].segments) == (0, 0)  # unknown


def test_without_a_path_nothing_is_recorded(tmp_path):
    """What the disabled setting does: the worker still calls record()."""
    history = TranslationHistory()
    assert not history.enabled
    record_one(history)
    assert history.entries() == []
    history.close()  # closing what was never opened is a no-op


def test_unopenable_db_disables_recording(tmp_path):
    in_the_way = tmp_path / "not-a-dir"
    in_the_way.write_text("")  # a file where the DB's directory should go
    history = TranslationHistory(str(in_the_way / "history.db"))
    assert not history.enabled
    record_one(history)  # must not raise: a batch outlives its history
    assert history.entries() == []


def test_a_failing_write_never_breaks_the_batch(tmp_path):
    """The files are already translated by the time the row is written, so a
    database that goes away mid-batch must not fail the run."""
    history = make_history(tmp_path)
    history._conn.execute("DROP TABLE history")
    history._conn.commit()
    record_one(history)  # no exception
    history.close()


def test_db_info_and_purge(tmp_path):
    history = make_history(tmp_path)
    record_one(history, "a.txt")
    record_one(history, "b.txt")
    history.close()

    db = str(tmp_path / "history.db")
    count, size = TranslationHistory.db_info(db)
    assert count == 2 and size > 0

    assert TranslationHistory.purge_db(db) == 2
    assert TranslationHistory.db_info(db)[0] == 0
    reopened = make_history(tmp_path)
    assert reopened.entries() == []
    reopened.close()


def test_db_info_and_purge_on_a_file_that_is_not_a_history(tmp_path):
    stray = tmp_path / "stray.db"
    stray.write_text("not a database")
    assert TranslationHistory.db_info(str(stray)) == (0, 0)
    assert TranslationHistory.purge_db(str(stray)) == 0


def test_db_info_on_a_missing_file(tmp_path):
    assert TranslationHistory.db_info(str(tmp_path / "nope.db")) == (0, 0)


def test_default_db_path_follows_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert TranslationHistory.default_db_path() == str(
        tmp_path / "argonaut" / "history.db"
    )


def test_default_db_path_falls_back_to_the_home_share_dir(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    path = TranslationHistory.default_db_path()
    assert path.endswith(os.path.join(".local", "share", "argonaut", "history.db"))


def test_the_schema_is_reused_by_a_second_opening(tmp_path):
    """CREATE TABLE IF NOT EXISTS: opening an existing file must not wipe it."""
    make_history(tmp_path).close()
    history = make_history(tmp_path)
    record_one(history)
    history.close()

    conn = sqlite3.connect(str(tmp_path / "history.db"))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    count = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    conn.close()
    assert "history" in tables and count == 1
