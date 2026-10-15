# argonaut-cli

[![PyPI](https://img.shields.io/pypi/v/argonaut-translator-cli)](https://pypi.org/project/argonaut-translator-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/argonaut-translator-cli)](https://pypi.org/project/argonaut-translator-cli/)
[![License: GPL-3.0](https://img.shields.io/github/license/Nibblex/argonaut-translator-cli)](LICENSE)

`argonaut-cli` translates documents — `.txt` `.docx` `.odt` `.odp` `.pptx`
`.epub` `.html` `.srt` `.pdf` — from the command line, without sending them
anywhere. Translation runs entirely on your own machine, with Argos
Translate or the heavier and more accurate NLLB-200, so it works with no
connection and your documents never leave the computer.

It is the engine of [Argonaut](https://github.com/Nibblex/argonaut-translator-gui), the
desktop application, on its own: install it alone for the terminal and for
scripts, or install the desktop application, which brings it along. The two
share the language packages, the NLLB model, the translation cache and the
history, so a document translated in one is answered from the cache in the
other.

This page is the user manual. If you want to build or contribute, see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Installing

```bash
pip install argonaut-translator-cli
```

That provides the `argonaut-cli` command. You need Python 3.10 or newer.

Translation needs at least one language package. List the ones on offer
and install the pairs you need, for example English to Spanish:

```bash
argonaut-cli packages list
argonaut-cli packages install en-es
```

## Translating

```bash
argonaut-cli translate report.pdf --to es
```

Each translation is written next to its original, named after it with the
target language's code — `report.pdf` becomes `report_es.pdf` — or into the
folder given with `-o`. The language of each document is detected on its
own, so documents in different languages can go in the same run; give it
with `--from` when you know it. A folder is walked recursively, and every
supported document in it is translated.

| Option | |
|---|---|
| `--to CODE` | Target language (required). `argonaut-cli languages` lists the codes. |
| `--from CODE` | Source language, instead of detecting each document's. |
| `--engine argos\|nllb` | Which engine translates (see below). Argos by default. |
| `-o`, `--output-dir DIR` | Write the translations here; created if missing. |
| `--skip-existing` | Leave alone a document whose translation already exists. |
| `--fast` | Greedy decoding: about 1.7 times faster, slightly less accurate. |
| `--threads N` | CPU threads for the engine. Every core by default. |
| `--no-cache` | Neither read nor write the shared translation cache. |
| `--no-history` | Do not record the run in the history. |
| `-q`, `--quiet` | Print only failures and the translated paths. |

The path of every translated document goes to standard output, and
progress and errors to standard error, so a script can collect the results:

```bash
argonaut-cli translate notes/ --to fr --quiet > translated.txt
```

The exit status says how the run went: `0` when every document was
translated or skipped, `1` when at least one failed (the others are still
translated), `2` when the request itself cannot work — an unknown language
code, a pair with no model, a missing file — and `130` when interrupted.

Ctrl+C stops after the step under way, the way the desktop application's
Cancel button does: an engine call already started cannot be cut short,
so it can take a few seconds. Press it again to abort at once.

## Engines

**Argos Translate** is the default. It needs one package per language
pair. A pair with no package of its own is translated through a third
language — Albanian to Spanish goes through English with the usual
packages — which works but loses some accuracy on the way; the command
says when that happens.

**NLLB-200** is one model for every pair among its 32 languages, and
noticeably more accurate. It is downloaded once (about 630 MB):

```bash
argonaut-cli model download
argonaut-cli translate thesis.docx --to de --engine nllb
```

`argonaut-cli model status` says whether it is installed and how much room
it takes; `argonaut-cli model remove` deletes it.

## Languages and packages

```bash
argonaut-cli languages                  # installed Argos languages
argonaut-cli languages --engine nllb    # NLLB's languages
argonaut-cli packages list              # every package, and whether it is installed
argonaut-cli packages list --updates    # only the ones with a newer version
argonaut-cli packages update            # update them all (or name pairs: en-es)
argonaut-cli packages remove en-es
```

## Cache and history

A paragraph translated once is kept, so repeating it — in the same
document, in another file of the run, or in a later run — costs nothing. The
end of each run says how much the cache answered. The history records every
document translated: when, from where to where, with which engine and how
long it took.

```bash
argonaut-cli cache info
argonaut-cli history list -n 20
argonaut-cli cache clear
argonaut-cli history clear
```

Clearing asks first; in a script, pass `--yes`. Both live under
`~/.local/share/argonaut/` (or `$XDG_DATA_HOME/argonaut/`) and are the
desktop application's own. The command line never discards old entries by
itself: how long they are kept is a setting of the desktop application.
