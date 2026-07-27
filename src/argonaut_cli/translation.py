"""Translation utilities: language detection, supported formats, the
two-level segment cache (in-memory and persistent sqlite) and the
ITranslation wrapper that reports progress, reuses cached segments and
allows cancelling."""

import hashlib
import os
import sqlite3
import time

from argostranslatefiles import argostranslatefiles
from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException

DetectorFactory.seed = 0

# langdetect codes that don't match the Argos ones
LANGDETECT_TO_ARGOS = {"zh-cn": "zh", "zh-tw": "zt"}

# What SentencePiece decodes the engine's unknown token into. NLLB used to
# reach for it on typographic punctuation it would not reproduce — curly
# quotes, apostrophes, dashes — leaving a "⁇" the reader sees as "??". The
# engine is now told to refuse that token, but caches filled beforehand
# still hold the damaged text, and serving it would keep the defect alive
# for as long as the cache does.
UNKNOWN_MARKER = "⁇"


def is_usable(translation):
    """False for a translation carrying the engine's unknown marker."""
    return UNKNOWN_MARKER not in translation


class CancelledError(Exception):
    pass


def check_cancelled(is_cancelled):
    """Aborts the current translation if the user asked to stop. Cancelling
    is cooperative — an engine call already in flight cannot be interrupted —
    so callers check this between units of work."""
    if is_cancelled():
        raise CancelledError()


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
        # a few paragraphs are plenty to detect a language; running the
        # detector over a whole book would dwarf the extraction itself
        code = detect(sample[:5000])
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


class TranslationCache:
    """A two-level segment cache: L1 is an in-memory dict (fast, session-scoped);
    L2 is an optional sqlite database (persistent across sessions).

    ``hits`` counts segments served without touching the engine, ``misses``
    those translated for the first time; together they let the window show
    how much a batch benefits from files sharing content.

    Pass ``db_path`` to enable persistence.  ``ttl_days`` sets how many days
    an entry can go unused before being pruned at open time (0 = never)."""

    # segment writes are committed in groups of this size: a crash loses at
    # most the last group, which only means retranslating those segments
    COMMIT_EVERY = 100

    def __init__(self, db_path=None, ttl_days=None):
        self._entries = {}
        self.hits = 0
        self.misses = 0
        self._conn = None
        self._dirty = 0  # writes since the last commit
        if db_path is not None:
            self._open_db(db_path, ttl_days or 0)

    @staticmethod
    def default_db_path():
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        return os.path.join(base, "argonaut", "translation_cache.db")

    def _open_db(self, path, ttl_days):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            # losing the last few segments on a crash only means
            # retranslating them: not worth an fsync per commit
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache "
                "(key_hash TEXT PRIMARY KEY, translation TEXT NOT NULL, "
                "last_used INTEGER NOT NULL DEFAULT 0)"
            )
            # migrate existing DB that lacks the last_used column
            try:
                self._conn.execute(
                    "ALTER TABLE cache ADD COLUMN "
                    "last_used INTEGER NOT NULL DEFAULT 0"
                )
                # stamp existing rows so they don't all expire on first TTL run
                self._conn.execute(
                    "UPDATE cache SET last_used = ? WHERE last_used = 0",
                    (int(time.time()),),
                )
            except sqlite3.OperationalError:
                pass  # column already exists
            # prune entries that haven't been used within the TTL window
            if ttl_days and ttl_days > 0:
                cutoff = int(time.time()) - ttl_days * 86400
                self._conn.execute(
                    "DELETE FROM cache WHERE last_used < ?", (cutoff,)
                )
            self._conn.commit()
        except Exception:  # noqa: BLE001
            self._conn = None  # fall back to in-memory only

    @staticmethod
    def _hash(key):
        return hashlib.sha256(repr(key).encode()).hexdigest()

    def lookup(self, key):
        """Returns (value, found). A found key counts as a hit, a missing
        one as a miss, so the caller need only translate on a miss. An entry
        holding the unknown marker is reported as missing: see
        :data:`UNKNOWN_MARKER`."""
        value = self._entries.get(key)
        if value is None and self._conn is not None:
            key_hash = self._hash(key)
            row = self._conn.execute(
                "SELECT translation FROM cache WHERE key_hash = ?", (key_hash,)
            ).fetchone()
            if row:
                value = row[0]
                if is_usable(value):
                    self._entries[key] = value  # promote to L1
                    self._conn.execute(
                        "UPDATE cache SET last_used = ? WHERE key_hash = ?",
                        (int(time.time()), key_hash),
                    )
                    self._commit_soon()
        if value is not None and is_usable(value):
            self.hits += 1
            return value, True
        if value is not None:
            # translating it again overwrites the stored copy, so a cache
            # filled before the fix repairs itself as it is used
            self._entries.pop(key, None)
        self.misses += 1
        return None, False

    def store(self, key, value):
        self._entries[key] = value
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key_hash, translation, last_used) "
                "VALUES (?, ?, ?)",
                (self._hash(key), value, int(time.time())),
            )
            self._commit_soon()

    def _commit_soon(self):
        """Counts a pending write and commits once a group is full."""
        self._dirty += 1
        if self._dirty >= self.COMMIT_EVERY:
            self._conn.commit()
            self._dirty = 0

    def close(self):
        """Releases the database connection and the in-memory entries. A
        batch holds every segment it translated, so a long session that never
        let go of them would keep whole books in memory. The hit and miss
        counters survive: the window still reports them afterwards."""
        self._entries = {}
        if self._conn is not None:
            self._conn.commit()  # flush the writes still waiting in a group
            self._conn.close()
            self._conn = None
            self._dirty = 0

    @property
    def reused(self):
        """Segments served from the cache instead of being retranslated."""
        return self.hits

    @staticmethod
    def db_info(db_path):
        """Returns (entry_count, size_bytes) for the cache DB, or (0, 0)."""
        try:
            size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            finally:
                conn.close()  # even when the file is not a usable cache
            return count, size
        except Exception:  # noqa: BLE001
            return 0, 0

    @staticmethod
    def purge_db(db_path):
        """Delete all entries and VACUUM the DB. Returns the deleted count."""
        try:
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
                conn.execute("DELETE FROM cache")
                conn.commit()           # must commit before VACUUM
                conn.execute("VACUUM")  # VACUUM cannot run inside a transaction
            finally:
                conn.close()  # even when the file is not a usable cache
            return count
        except Exception:  # noqa: BLE001
            return 0


class ProgressTranslation:
    """Wraps an ITranslation to report progress chunk by chunk, cache
    repeated texts and abort mid-file.

    Pass a shared ``cache`` (a :class:`TranslationCache`) to reuse
    translations across a whole batch so a repeated paragraph translates
    once no matter how many files contain it. Entries are namespaced by
    engine, model version and language pair: a file detected as a
    different source language never picks up another pair's translation,
    and upgrading a model stops serving the old model's output."""

    def __init__(self, inner, on_progress, is_cancelled, cache=None,
                 engine_id="", engine_version=""):
        self._inner = inner
        self._on_progress = on_progress
        self._is_cancelled = is_cancelled
        self._cache = TranslationCache() if cache is None else cache
        self._prefix = self._language_pair(engine_id, engine_version)
        self._done = 0
        self.reused = 0  # segments this file served from the cache
        # segments the cache was asked about: the denominator of the reuse
        # rate, so the chunks that never reach it (numbers, punctuation) are
        # not counted as misses
        self.segments = 0

    def _language_pair(self, engine_id="", engine_version=""):
        def code(lang):
            return getattr(lang, "code", "") if lang is not None else ""

        return (
            engine_id,
            engine_version,
            code(getattr(self._inner, "from_lang", None)),
            code(getattr(self._inner, "to_lang", None)),
        )

    def translate(self, text):
        return self.translate_many([text])[0]

    def translate_many(self, texts):
        """Translates a list of texts, serving whatever the cache already
        knows and asking the engine only for the rest — in a single batched
        call when the engine supports one, text by text otherwise."""
        results = [None] * len(texts)
        misses = []  # (index, text) pairs the cache could not answer

        for i, text in enumerate(texts):
            check_cancelled(self._is_cancelled)
            # chunks with no letters (numbers, punctuation) are left as-is;
            # recognising them is cheaper than a cache entry
            if not any(c.isalpha() for c in text):
                results[i] = text
                continue
            self.segments += 1
            result, found = self._cache.lookup((self._prefix, text))
            if found:
                results[i] = result
                self.reused += 1
            else:
                misses.append((i, text))

        if misses:
            # the same text can repeat within one call (headers, footers)
            # before the cache has seen it: translate it once and fill
            # every position that asked for it
            positions = {}
            for i, text in misses:
                positions.setdefault(text, []).append(i)
            for text, result in zip(positions, self._translate(list(positions))):
                self._cache.store((self._prefix, text), result)
                for i in positions[text]:
                    results[i] = result

        self._done += len(texts)
        self._on_progress(self._done)
        return results

    def _translate(self, texts):
        """Sends the cache misses to the engine, batched when it can."""
        check_cancelled(self._is_cancelled)
        if hasattr(self._inner, "translate_many"):
            return self._inner.translate_many(texts)
        # an engine without batching is called once per text, so cancelling
        # need not wait for the rest of the page
        results = []
        for text in texts:
            check_cancelled(self._is_cancelled)
            results.append(self._inner.translate(text))
        return results

    def __getattr__(self, name):
        return getattr(self._inner, name)
