# Contributing to argonaut-cli

[![CI](https://github.com/Nibblex/argonaut-translator-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/Nibblex/argonaut-translator-cli/actions/workflows/ci.yml)
[![Coverage](https://raw.githubusercontent.com/Nibblex/argonaut-translator-cli/badges/coverage.svg)](https://github.com/Nibblex/argonaut-translator-cli/actions/workflows/ci.yml)

`README.md` is the user manual, so it stays about using the command.
Everything about building, testing and releasing it is here.

## Running from a checkout

```bash
pip install -e .
argonaut-cli --help        # or: python -m argonaut_cli --help
```

Python 3.10 or newer (the pymupdf wheels need it). Translating also needs
at least one Argos language package: `argonaut-cli packages install en-es`.

## Tests

```bash
pip install -e .[test]     # pytest-cov is required
pytest                     # the whole suite
pytest tests/test_cli.py -o addopts=""   # one file, without coverage
```

`pyproject.toml` puts `--cov=argonaut_cli` in `addopts`, so a bare `pytest`
fails outright unless `pytest-cov` is installed — the `[test]` extra brings
it.

The suite needs no language models: `tests/conftest.py` sandboxes
`XDG_CONFIG_HOME` and `XDG_DATA_HOME` (per test, for the latter) and fakes
the engines with `FakeLanguage`, `FakeTranslation` and
`FakeBatchTranslation`.

CI runs the suite on Python 3.10 through 3.14; that matrix, `requires-python`
and the classifiers in `pyproject.toml` have to stay in step.

## Layout

All modules live in the `src/argonaut_cli/` package:

- `__init__.py` — package version (`__version__`).
- `cli.py` — the `argonaut-cli` command: argument parsing, the console
  report of a batch, and the package, model, history and cache commands.
- `batch.py` — translates a list of documents into one language and reports
  to a `Listener`: detection, the shared cache, skipping existing outputs,
  the history and cancellation. The documents it turns down raise a
  `FileError` subclass that carries the file and the languages.
- `translation.py` — language detection, supported formats, the two-level
  segment cache and `ProgressTranslation`, the wrapper that reports
  progress, reuses cached segments and cancels mid-file.
- `pdf.py` — PDF translation (whole paragraphs, progress, cancellation).
- `typesetting.py` — lays the translation back into the page: line
  breaking, fitting each paragraph to its box, and a font per script.
- `nllb.py` — the NLLB-200 backend (CTranslate2 + SentencePiece), exposing
  the same duck-typed API as argostranslate, so the two engines are
  interchangeable everywhere else.
- `packages.py` — Argos package index, download, installation and removal.
- `download.py` — shared streaming download with progress and cancellation.
- `history.py` — persistent record of the files each batch translated.

## The desktop application builds on this package

[Argonaut](https://github.com/Nibblex/Argonaut) (`argonaut-translator-gui`)
imports `batch`, `translation`, `history`, `nllb`, `packages` and `pdf`, and
requires `argonaut-translator-cli>=2,<3`. So:

- those modules are a public interface, not only the command: a change
  that breaks them needs a new major version, and a desktop release that
  follows it;
- the data under `$XDG_DATA_HOME/argonaut/` — the cache, the history and
  the NLLB model — is shared with the desktop application and must not move;
- user-facing text in the engine stays English and comes as data where the
  desktop application needs its own words: `FileError` subclasses carry the
  file and the languages, and the desktop application renders them in the
  interface language.

## Releasing

Work reaches `main` through branches, not directly: a topic branch merges
`--no-ff` into `dev`, `dev` merges `--no-ff` into `main`, and the annotated
tag goes on `main` **after** that merge, so it points at the tree actually
released rather than at the branch head before it.

Bump `__version__` in `src/argonaut_cli/__init__.py`; `pyproject.toml`
reads the version from there, so nothing else needs touching.

```bash
git checkout dev  && git merge --no-ff my-branch && git push origin dev
git checkout main && git merge --no-ff dev       && git push origin main
git tag -a v2.0.0 -m "argonaut-cli 2.0.0"
git push origin v2.0.0
gh release create v2.0.0 --title "argonaut-cli 2.0.0" --notes-file notes.md
```

Publishing the release triggers `.github/workflows/publish.yml`, which builds
the wheel and the sdist and uploads them via trusted publishing. Pushing the
tag alone publishes nothing, so the CI run on `main` can be used as a gate: a
version is permanent on PyPI once uploaded. CI never runs on `dev` — it
watches `main` and pull requests only — so the local suite is what gates the
merge.

A desktop release that needs something new from here has to wait until
this package is on PyPI: its CI installs the engine from there.
