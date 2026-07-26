"""Persistent record of the files a batch translated.

Where the translation cache stores *segments* so they need not be
translated twice, the history stores *files*: what was translated, into
what, where the result was written and how long it took. It is written
once per finished file, so it costs nothing measurable next to the
translation itself, and it survives between sessions in its own sqlite
database.
"""

import os
import sqlite3
import time
from collections import namedtuple

# one finished translation, as the dialog shows it
Entry = namedtuple(
    "Entry",
    "translated_at source_path output_path from_code to_code engine seconds",
)


class TranslationHistory:
    """A sqlite log of finished translations, newest first.

    Pass ``db_path`` to open it; ``ttl_days`` sets how many days an entry
    is kept before being pruned at open time (0 = forever). Without a
    path nothing is recorded, which is what the disabled setting does."""

    def __init__(self, db_path=None, ttl_days=None):
        self._conn = None
        if db_path is not None:
            self._open_db(db_path, ttl_days or 0)

    @staticmethod
    def default_db_path():
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        return os.path.join(base, "argonaut", "history.db")

    def _open_db(self, path, ttl_days):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            # one row per translated file: losing the last few on a crash
            # costs nothing, the files themselves are already written
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "translated_at INTEGER NOT NULL, "
                "source_path TEXT NOT NULL, "
                "output_path TEXT NOT NULL, "
                "from_code TEXT NOT NULL, "
                "to_code TEXT NOT NULL, "
                "engine TEXT NOT NULL, "
                "seconds REAL NOT NULL)"
            )
            # the dialog always reads in date order
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS history_by_date "
                "ON history (translated_at DESC)"
            )
            if ttl_days and ttl_days > 0:
                cutoff = int(time.time()) - ttl_days * 86400
                self._conn.execute(
                    "DELETE FROM history WHERE translated_at < ?", (cutoff,)
                )
            self._conn.commit()
        except Exception:  # noqa: BLE001
            self._conn = None  # an unusable database disables recording

    @property
    def enabled(self):
        """False when nothing is being recorded, either because history is
        off or because its database could not be opened."""
        return self._conn is not None

    def record(self, source_path, output_path, from_code, to_code,
               engine, seconds, when=None):
        """Logs one finished translation. Never raises: a history that
        cannot be written must not fail the batch that produced it."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO history (translated_at, source_path, output_path, "
                "from_code, to_code, engine, seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time() if when is None else when),
                    source_path, output_path, from_code, to_code,
                    engine, float(seconds),
                ),
            )
            self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def entries(self, limit=None):
        """The recorded translations, newest first."""
        if self._conn is None:
            return []
        query = (
            "SELECT translated_at, source_path, output_path, from_code, "
            "to_code, engine, seconds FROM history ORDER BY translated_at DESC, "
            "id DESC"
        )
        params = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        return [Entry(*row) for row in self._conn.execute(query, params)]

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def db_info(db_path):
        """Returns (entry_count, size_bytes) for the history DB, or (0, 0)."""
        try:
            size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            finally:
                conn.close()  # even when the file is not a usable history
            return count, size
        except Exception:  # noqa: BLE001
            return 0, 0

    @staticmethod
    def purge_db(db_path):
        """Deletes every entry and VACUUMs the DB. Returns the deleted count."""
        try:
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
                conn.execute("DELETE FROM history")
                conn.commit()           # must commit before VACUUM
                conn.execute("VACUUM")  # VACUUM cannot run inside a transaction
            finally:
                conn.close()  # even when the file is not a usable history
            return count
        except Exception:  # noqa: BLE001
            return 0
