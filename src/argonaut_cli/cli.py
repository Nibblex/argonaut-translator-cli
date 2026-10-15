"""Command line: translate documents offline, and manage what the engines
need — language packages, the NLLB model, the cache and the history.

It shares the engine, the cache, the history and the models with the
window, so a document translated from one is answered from the cache in
the other. It never prunes either database on its own: how long entries
are kept is a setting of the window, and clearing them here is always an
explicit command."""

import argparse
import os
import signal
import sys
import time

import argostranslate.settings
import argostranslate.translate

from argonaut import __version__, nllb, packages
from argonaut.batch import Batch, Listener, documents_in
from argonaut.history import TranslationHistory
from argonaut.translation import SUPPORTED_EXTS, CancelledError, TranslationCache

PROG = "argonaut-cli"
ENGINES = ("argos", "nllb")

# exit statuses besides 0: a document failed, the request itself was wrong,
# and the run was interrupted (the shell's own code for SIGINT)
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


class CliError(Exception):
    """Something wrong with the request itself, reported in one line."""


def human_size(n):
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n / 1024:.1f} GB"


def format_duration(seconds):
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes:02d}:{secs:02d}"


def default_threads():
    # every core, as the window does: translation is what the user waits for
    return os.cpu_count() or 1


def load_languages(engine, threads=None, fast=False):
    """The installed languages of an engine, set up for ``threads`` CPU
    threads and, with ``fast``, greedy decoding instead of beam search."""
    if engine == "nllb":
        if not nllb.is_model_installed():
            raise CliError(
                f"the NLLB model is not installed; get it with: {PROG} model download"
            )
        return nllb.get_installed_languages(
            threads=threads,
            beam_size=1 if fast else nllb.NllbEngine.DEFAULT_BEAM,
        )
    if threads:
        argostranslate.settings.intra_threads = threads
    if fast:
        argostranslate.settings.beam_size = 1
    return argostranslate.translate.get_installed_languages()


def find_language(languages, code, engine):
    for lang in languages:
        if lang.code == code:
            return lang
    codes = ", ".join(sorted(lang.code for lang in languages)) or "none"
    raise CliError(f"no {engine} language with code '{code}' (installed: {codes})")


def pivot_language(translation):
    """The language a pair goes through when Argos has no package for it,
    or None for a pair it translates directly."""
    if isinstance(translation, argostranslate.translate.CompositeTranslation):
        return translation.t1.to_lang
    return None


class Interrupt:
    """SIGINT handling for a long run: the first Ctrl+C asks the batch to
    stop at its next safe point, the way the window's Cancel button does;
    a second one aborts at once."""

    def __init__(self, stream):
        self.requested = False
        self._stream = stream

    def __call__(self, signum, frame):
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        print(
            "\nCancelling after the current step (Ctrl+C again to abort)…",
            file=self._stream,
        )

    def is_set(self):
        return self.requested


class ConsoleReport(Listener):
    """Prints a batch as it runs: a line per file on stderr, the path of
    every translated document on stdout (so a script can collect them), and
    on a terminal a live progress line that is overwritten in place."""

    def __init__(self, files, out=None, err=None, quiet=False, live=None):
        self.files = files
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.quiet = quiet
        self.live = self.err.isatty() if live is None else live
        self.translated = self.skipped = self.failed = 0
        self.batch_reused = self.batch_segments = 0
        self._live_text = ""

    def _say(self, text):
        if not self.quiet:
            self._clear_live()
            print(text, file=self.err, flush=True)

    def _show_live(self, text):
        if self.live and not self.quiet:
            padding = " " * max(0, len(self._live_text) - len(text))
            self.err.write(f"\r{text}{padding}")
            self.err.flush()
            self._live_text = text

    def _clear_live(self):
        if self._live_text:
            self.err.write("\r" + " " * len(self._live_text) + "\r")
            self.err.flush()
            self._live_text = ""

    def on_file_started(self, index, path):
        self._say(f"[{index + 1}/{len(self.files)}] {os.path.basename(path)}")

    def on_language_detected(self, index, lang):
        self._say(f"  detected {lang}")

    def on_progress(self, done, total):
        if total:
            self._show_live(f"  {100 * done // total}% ({done}/{total})")

    def on_phase(self, key, kwargs):
        labels = {
            "reading": "reading page",
            "translating_page": "translating page",
            "generating": "writing page",
        }
        if key in labels:
            self._show_live(f"  {labels[key]} {kwargs['done']}/{kwargs['total']}")
        elif key == "saving":
            self._show_live("  saving")

    def on_file_done(self, index, out_path, seconds):
        self.translated += 1
        self._say(f"  translated in {format_duration(seconds)}")
        print(out_path, file=self.out, flush=True)

    def on_file_failed(self, index, error):
        self.failed += 1
        self._clear_live()
        # failures are reported even when quiet: they change the exit status
        print(f"  failed: {error}", file=self.err, flush=True)

    def on_file_skipped(self, index, out_path):
        self.skipped += 1
        self._say(f"  skipped: {os.path.basename(out_path)} already exists")

    def on_cache_stats(self, index, file_reused, batch_reused, batch_segments):
        self.batch_reused = batch_reused
        self.batch_segments = batch_segments

    def summary(self, seconds, cancelled):
        parts = [f"{self.translated} translated"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
        text = f"{', '.join(parts)} of {len(self.files)} in {format_duration(seconds)}"
        if cancelled:
            text = f"Cancelled: {text}"
        if self.batch_reused and self.batch_segments:
            percent = round(100 * self.batch_reused / self.batch_segments)
            text += (
                f"; the cache answered {self.batch_reused} of"
                f" {self.batch_segments} segments ({percent}%)"
            )
        self._say(text + ".")


def cmd_translate(args, out, err):
    paths = [os.path.abspath(path) for path in args.paths]
    for path in paths:
        if not os.path.exists(path):
            raise CliError(f"no such file or folder: {path}")
        if not os.path.isdir(path):
            ext = os.path.splitext(path)[1].lower()
            if ext not in SUPPORTED_EXTS:
                raise CliError(
                    f"unsupported format: {os.path.basename(path)}"
                    f" (supported: {' '.join(SUPPORTED_EXTS)})"
                )
    files = list(documents_in(paths))
    if not files:
        raise CliError("no supported documents in the given folders")
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)

    if args.threads is not None and args.threads < 1:
        raise CliError("--threads needs at least one thread")
    threads = args.threads or default_threads()
    languages = load_languages(args.engine, threads=threads, fast=args.fast)
    if not languages:
        raise CliError(
            f"no languages installed; install a package with: {PROG} packages install"
        )
    dst = find_language(languages, args.to, args.engine)
    src = find_language(languages, args.source, args.engine) if args.source else None
    if src is not None:
        if src is dst:
            raise CliError("the source and target languages are the same")
        translation = src.get_translation(dst)
        if translation is None:
            raise CliError(f"no model {src} → {dst}")
        via = pivot_language(translation) if args.engine == "argos" else None
        if via is not None and not args.quiet:
            print(
                f"note: there is no {src} → {dst} package, so the text goes"
                f" through {via} and loses some accuracy on the way",
                file=err,
            )

    report = ConsoleReport(files, out=out, err=err, quiet=args.quiet)
    interrupt = Interrupt(err)
    previous = signal.signal(signal.SIGINT, interrupt)
    started = time.monotonic()
    try:
        Batch(
            src, dst, languages, files,
            output_dir=args.output_dir and os.path.abspath(args.output_dir),
            skip_existing=args.skip_existing,
            engine_id=args.engine,
            greedy=args.fast,
            cache_db_path=None if args.no_cache else TranslationCache.default_db_path(),
            history_db_path=(
                None if args.no_history else TranslationHistory.default_db_path()
            ),
            listener=report,
            is_cancelled=interrupt.is_set,
        ).run()
    finally:
        signal.signal(signal.SIGINT, previous)
    report.summary(time.monotonic() - started, interrupt.requested)
    if interrupt.requested:
        return EXIT_INTERRUPTED
    return EXIT_FAILED if report.failed else 0


def cmd_languages(args, out, err):
    if args.engine == "nllb" and not nllb.is_model_installed():
        print(
            f"note: the NLLB model is not installed ({PROG} model download)",
            file=err,
        )
        languages = [(code, name) for code, name, _ in nllb.LANGUAGES]
    else:
        languages = [
            (lang.code, str(lang)) for lang in load_languages(args.engine)
        ]
    for code, name in sorted(languages, key=lambda item: item[1].casefold()):
        print(f"{code:<4} {name}", file=out)
    return 0


def parse_pair(text):
    """"en-es" -> ("en", "es")."""
    source, sep, target = text.partition("-")
    if not sep or not source or not target:
        raise CliError(f"a pair is written FROM-TO, like en-es: {text!r}")
    return source.lower(), target.lower()


def package_state(pkg, versions):
    installed = versions.get(packages.pair(pkg))
    if installed is None:
        return "not installed"
    if packages.is_newer(pkg.package_version, installed):
        return f"update available ({installed} → {pkg.package_version})"
    return "installed"


def fetch_index():
    try:
        return packages.get_available()
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"could not fetch the package index: {exc}") from exc


def download_progress(err):
    """A (done, total) byte callback that redraws one line on a terminal."""
    if not err.isatty():
        return None

    def report(done, total):
        err.write(f"\r  {done >> 20}/{max(1, total >> 20)} MB")
        err.flush()

    return report


def install_packages(pkgs, verb, err):
    failures = 0
    for pkg in pkgs:
        print(f"{verb} {packages.pair_text(pkg)} ({pkg.package_version})…", file=err)
        try:
            packages.install(pkg, on_progress=download_progress(err))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"\r  failed: {exc}", file=err)
        else:
            if err.isatty():
                err.write("\r" + " " * 24 + "\r")
    return EXIT_FAILED if failures else 0


def cmd_packages_list(args, out, err):
    versions = packages.installed_versions()
    for pkg in fetch_index():
        state = package_state(pkg, versions)
        if args.installed and state == "not installed":
            continue
        if args.updates and not state.startswith("update"):
            continue
        print(
            f"{pkg.from_code}-{pkg.to_code:<8} {packages.pair_text(pkg):<40} {state}",
            file=out,
        )
    return 0


def cmd_packages_install(args, out, err):
    wanted = [parse_pair(text) for text in args.pairs]
    index = {packages.pair(pkg): pkg for pkg in fetch_index()}
    versions = packages.installed_versions()
    to_install = []
    for pair in wanted:
        if pair not in index:
            raise CliError(f"no package for {pair[0]}-{pair[1]} in the index")
        if pair in versions and not packages.is_newer(
            index[pair].package_version, versions[pair]
        ):
            print(f"{packages.pair_text(index[pair])} is already installed", file=err)
            continue
        to_install.append(index[pair])
    return install_packages(to_install, "Installing", err)


def cmd_packages_update(args, out, err):
    versions = packages.installed_versions()
    wanted = {parse_pair(text) for text in args.pairs}
    for pair in wanted:
        if pair not in versions:
            raise CliError(f"{pair[0]}-{pair[1]} is not installed")
    outdated = [
        pkg
        for pkg in fetch_index()
        if packages.pair(pkg) in versions
        and (not wanted or packages.pair(pkg) in wanted)
        and packages.is_newer(pkg.package_version, versions[packages.pair(pkg)])
    ]
    if not outdated:
        print("Everything is up to date.", file=err)
        return 0
    return install_packages(outdated, "Updating", err)


def cmd_packages_remove(args, out, err):
    versions = packages.installed_versions()
    pairs = [parse_pair(text) for text in args.pairs]
    for pair in pairs:
        if pair not in versions:
            raise CliError(f"{pair[0]}-{pair[1]} is not installed")
    for pair in pairs:
        packages.uninstall(pair)
        print(f"Removed {pair[0]}-{pair[1]}", file=err)
    return 0


def cmd_model_status(args, out, err):
    if not nllb.is_model_installed():
        print(f"not installed (about {nllb.MODEL_SIZE_MB} MB)", file=out)
        return 0
    path = nllb.model_dir()
    size = sum(
        os.path.getsize(os.path.join(root, name))
        for root, _, names in os.walk(path)
        for name in names
    )
    print(f"installed in {path} ({human_size(size)})", file=out)
    return 0


def cmd_model_download(args, out, err):
    if nllb.is_model_installed():
        print("The NLLB model is already installed.", file=err)
        return 0
    print(f"Downloading the NLLB-200 model (about {nllb.MODEL_SIZE_MB} MB)…", file=err)
    interrupt = Interrupt(err)
    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        nllb.download_model(
            on_progress=download_progress(err), is_cancelled=interrupt.is_set
        )
    except CancelledError:
        print("\nCancelled.", file=err)
        return EXIT_INTERRUPTED
    finally:
        signal.signal(signal.SIGINT, previous)
    print("\nDone.", file=err)
    return 0


def cmd_model_remove(args, out, err):
    if not nllb.is_model_installed():
        print("The NLLB model is not installed.", file=err)
        return 0
    nllb.remove_model()
    print("Removed the NLLB model.", file=err)
    return 0


def confirm(question, args, err):
    """Asks before deleting, unless --yes was given. Without a terminal to
    ask on, nothing is deleted: a script has to say --yes."""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise CliError("refusing to delete without --yes when not run interactively")
    print(f"{question} [y/N] ", end="", file=err, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def cmd_history_list(args, out, err):
    path = TranslationHistory.default_db_path()
    if not os.path.exists(path):
        return 0
    history = TranslationHistory(path, ttl_days=0)
    try:
        entries = history.entries(limit=args.limit)
    finally:
        history.close()
    for entry in entries:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.translated_at))
        line = (
            f"{when}  {entry.from_code}→{entry.to_code}  {entry.engine:<5}"
            f"  {format_duration(entry.seconds)}  {entry.source_path}"
            f" → {entry.output_path}"
        )
        if entry.segments:
            line += f"  (cache {round(100 * entry.reused / entry.segments)}%)"
        print(line, file=out)
    return 0


def cmd_history_clear(args, out, err):
    path = TranslationHistory.default_db_path()
    count, _ = TranslationHistory.db_info(path)
    if not count:
        print("The history is empty.", file=err)
        return 0
    if not confirm(f"Delete the {count} entries of the history?", args, err):
        return 0
    print(f"Deleted {TranslationHistory.purge_db(path)} entries.", file=err)
    return 0


def cmd_cache_info(args, out, err):
    path = TranslationCache.default_db_path()
    count, size = TranslationCache.db_info(path)
    print(f"{count} segments, {human_size(size)} ({path})", file=out)
    return 0


def cmd_cache_clear(args, out, err):
    path = TranslationCache.default_db_path()
    count, _ = TranslationCache.db_info(path)
    if not count:
        print("The cache is empty.", file=err)
        return 0
    if not confirm(f"Delete the {count} cached segments?", args, err):
        return 0
    print(f"Deleted {TranslationCache.purge_db(path)} segments.", file=err)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Translate documents offline with Argos Translate or NLLB-200.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    translate = commands.add_parser(
        "translate",
        help="translate documents",
        description=(
            "Translate documents, or every supported document in a folder."
            " Each translation is written next to its original as"
            " NAME_<code>.EXT, or into --output-dir; the path of every"
            " translated document is printed on stdout."
        ),
    )
    translate.add_argument("paths", nargs="+", metavar="PATH", help="documents or folders")
    translate.add_argument("--to", required=True, metavar="CODE", help="target language code")
    translate.add_argument(
        "--from", dest="source", metavar="CODE",
        help="source language code (default: detect each document's language)",
    )
    translate.add_argument("--engine", choices=ENGINES, default="argos")
    translate.add_argument("-o", "--output-dir", metavar="DIR")
    translate.add_argument(
        "--skip-existing", action="store_true",
        help="leave documents whose translation already exists",
    )
    translate.add_argument(
        "--fast", action="store_true",
        help="greedy decoding: about 1.7 times faster, slightly less accurate",
    )
    translate.add_argument(
        "--threads", type=int, metavar="N",
        help=f"CPU threads for the engine (default: all {default_threads()})",
    )
    translate.add_argument(
        "--no-cache", action="store_true",
        help="neither read nor write the shared translation cache",
    )
    translate.add_argument(
        "--no-history", action="store_true", help="do not record the run in the history"
    )
    translate.add_argument(
        "-q", "--quiet", action="store_true", help="print only failures and output paths"
    )
    translate.set_defaults(func=cmd_translate)

    languages = commands.add_parser("languages", help="list the installed languages")
    languages.add_argument("--engine", choices=ENGINES, default="argos")
    languages.set_defaults(func=cmd_languages)

    pkg = commands.add_parser(
        "packages", help="manage Argos language packages"
    ).add_subparsers(dest="action", required=True, metavar="ACTION")
    listing = pkg.add_parser("list", help="list the packages in the index")
    which = listing.add_mutually_exclusive_group()
    which.add_argument("--installed", action="store_true", help="only installed ones")
    which.add_argument("--updates", action="store_true", help="only ones with an update")
    listing.set_defaults(func=cmd_packages_list)
    for name, func, text in (
        ("install", cmd_packages_install, "install packages, like en-es"),
        ("remove", cmd_packages_remove, "remove installed packages"),
    ):
        action = pkg.add_parser(name, help=text)
        action.add_argument("pairs", nargs="+", metavar="FROM-TO")
        action.set_defaults(func=func)
    update = pkg.add_parser("update", help="update installed packages (all by default)")
    update.add_argument("pairs", nargs="*", metavar="FROM-TO")
    update.set_defaults(func=cmd_packages_update)

    model = commands.add_parser(
        "model", help="manage the NLLB-200 model"
    ).add_subparsers(dest="action", required=True, metavar="ACTION")
    for name, func, text in (
        ("status", cmd_model_status, "say whether the model is installed"),
        ("download", cmd_model_download, f"download it (about {nllb.MODEL_SIZE_MB} MB)"),
        ("remove", cmd_model_remove, "delete it from disk"),
    ):
        model.add_parser(name, help=text).set_defaults(func=func)

    history = commands.add_parser(
        "history", help="the record of translated documents"
    ).add_subparsers(dest="action", required=True, metavar="ACTION")
    history_list = history.add_parser("list", help="newest first")
    history_list.add_argument("-n", "--limit", type=int, metavar="N")
    history_list.set_defaults(func=cmd_history_list)
    history_clear = history.add_parser("clear", help="delete every entry")
    history_clear.add_argument("-y", "--yes", action="store_true")
    history_clear.set_defaults(func=cmd_history_clear)

    cache = commands.add_parser(
        "cache", help="the translation cache shared with the window"
    ).add_subparsers(dest="action", required=True, metavar="ACTION")
    cache.add_parser("info", help="its size").set_defaults(func=cmd_cache_info)
    cache_clear = cache.add_parser("clear", help="delete every cached segment")
    cache_clear.add_argument("-y", "--yes", action="store_true")
    cache_clear.set_defaults(func=cmd_cache_clear)
    return parser


def main(argv=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return args.func(args, out, err)
    except CliError as exc:
        print(f"{PROG}: error: {exc}", file=err)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\nAborted.", file=err)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
