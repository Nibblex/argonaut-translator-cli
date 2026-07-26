"""Optional NLLB-200 backend (CTranslate2 + SentencePiece).

Exposes the same duck-typed API the rest of the app already consumes from
argostranslate — Language objects with .code/.get_translation() and
translations with .translate()/.from_lang/.to_lang — so both engines are
interchangeable. The model (~630 MB, int8) is downloaded on demand from
Hugging Face; ctranslate2 and sentencepiece are already dependencies of
Argos Translate, so no new packages are required.
"""

import os
import re
import shutil
import urllib.request

from argonaut.download import download_to

MODEL_REPO = "JustFrederik/nllb-200-distilled-600M-ct2-int8"
MODEL_BASE_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/main"
MODEL_FILES = [
    "config.json",
    "shared_vocabulary.txt",
    "sentencepiece.bpe.model",
    "model.bin",
]
MODEL_SIZE_MB = 630  # approximate total, shown in the download prompt

# langdetect-compatible code, English name, FLORES-200 code
LANGUAGES = [
    ("ar", "Arabic", "arb_Arab"),
    ("bg", "Bulgarian", "bul_Cyrl"),
    ("ca", "Catalan", "cat_Latn"),
    ("cs", "Czech", "ces_Latn"),
    ("da", "Danish", "dan_Latn"),
    ("de", "German", "deu_Latn"),
    ("el", "Greek", "ell_Grek"),
    ("en", "English", "eng_Latn"),
    ("es", "Spanish", "spa_Latn"),
    ("fi", "Finnish", "fin_Latn"),
    ("fr", "French", "fra_Latn"),
    ("he", "Hebrew", "heb_Hebr"),
    ("hi", "Hindi", "hin_Deva"),
    ("hu", "Hungarian", "hun_Latn"),
    ("id", "Indonesian", "ind_Latn"),
    ("it", "Italian", "ita_Latn"),
    ("ja", "Japanese", "jpn_Jpan"),
    ("ko", "Korean", "kor_Hang"),
    ("nl", "Dutch", "nld_Latn"),
    ("no", "Norwegian", "nob_Latn"),
    ("pl", "Polish", "pol_Latn"),
    ("pt", "Portuguese", "por_Latn"),
    ("ro", "Romanian", "ron_Latn"),
    ("ru", "Russian", "rus_Cyrl"),
    ("sk", "Slovak", "slk_Latn"),
    ("sv", "Swedish", "swe_Latn"),
    ("th", "Thai", "tha_Thai"),
    ("tr", "Turkish", "tur_Latn"),
    ("uk", "Ukrainian", "ukr_Cyrl"),
    ("vi", "Vietnamese", "vie_Latn"),
    ("zh", "Chinese", "zho_Hans"),
    ("zt", "Chinese (traditional)", "zho_Hant"),
]

_SENTENCE_RE = re.compile(r"(?<=[.!?…。！？])[ \t]+")
_PARAGRAPH_RE = re.compile(r"(\n+)")


def model_dir():
    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return os.path.join(base, "argonaut", "nllb-200-distilled-600M-ct2")


def is_model_installed(path=None):
    path = path or model_dir()
    files = (os.path.join(path, name) for name in MODEL_FILES)
    # an empty file is a broken download, not an installed model
    return all(os.path.exists(f) and os.path.getsize(f) > 0 for f in files)


def download_model(path=None, base_url=None, on_progress=None, is_cancelled=None):
    """Downloads the model files to `path`, reporting (done_bytes,
    total_bytes) after each chunk. Cancelling removes the partial file."""
    path = path or model_dir()
    base_url = base_url or MODEL_BASE_URL
    os.makedirs(path, exist_ok=True)

    # every file is opened up front so the total size is known from the
    # first progress report
    responses = []
    try:
        for name in MODEL_FILES:
            responses.append(urllib.request.urlopen(f"{base_url}/{name}"))
        total = sum(int(r.headers.get("Content-Length") or 0) for r in responses)
        done = 0
        for name, response in zip(MODEL_FILES, responses):
            done = download_to(
                response, os.path.join(path, name),
                on_progress, is_cancelled, done=done, total=total,
            )
    finally:
        for response in responses:
            response.close()  # harmless on the ones download_to closed


def remove_model(path=None):
    """Deletes the downloaded model directory to free disk space."""
    path = path or model_dir()
    if os.path.isdir(path):
        shutil.rmtree(path)


class NllbEngine:
    """Shared CTranslate2 translator, loaded lazily on first use."""

    DEFAULT_BEAM = 2

    def __init__(self, path=None, threads=None, beam_size=DEFAULT_BEAM):
        self.path = path or model_dir()
        self.threads = threads
        self.beam_size = beam_size
        self._translator = None
        self._sp = None

    @staticmethod
    def thread_split(threads):
        """Splits a thread budget into (inter, intra): parallel CTranslate2
        workers of ~4 threads each. Benchmarked on batch translation: with 8
        threads 2x4 beats 1x8 by ~10%, with 16 4x4 beats 1x16 by ~16%, and
        below 8 a single worker wins, so small budgets are never split."""
        inter = min(4, threads // 4) or 1
        return inter, max(1, threads // inter)

    def _load(self):
        if self._translator is None:
            import ctranslate2
            import sentencepiece

            inter, intra = self.thread_split(
                self.threads or os.cpu_count() or 4
            )
            self._translator = ctranslate2.Translator(
                self.path,
                device="cpu",
                inter_threads=inter,
                intra_threads=intra,
            )
            self._sp = sentencepiece.SentencePieceProcessor(
                os.path.join(self.path, "sentencepiece.bpe.model")
            )

    def translate_batch(self, sentences, src_flores, dst_flores):
        self._load()
        source = [
            [src_flores] + self._sp.encode(s, out_type=str) + ["</s>"]
            for s in sentences
        ]
        results = self._translator.translate_batch(
            source,
            target_prefix=[[dst_flores]] * len(source),
            beam_size=self.beam_size,
            max_batch_size=1024,
            batch_type="tokens",
            # the model reaches for the unknown token on typographic
            # punctuation it will not reproduce — curly quotes, apostrophes,
            # dashes — and SentencePiece decodes it as "⁇", which lands in
            # the translation looking like two question marks. Refusing the
            # token makes it pick the next real one instead: "de “riot” y"
            # comes back as "de Riot y" rather than "de ⁇ riot ⁇ y"
            disable_unk=True,
        )
        translated = []
        for result in results:
            tokens = result.hypotheses[0]
            if tokens and tokens[0] == dst_flores:
                tokens = tokens[1:]
            translated.append(self._sp.decode(tokens))
        return translated


def split_sentences(text):
    """NLLB is a sentence-level model: multi-sentence paragraphs are split
    so each piece is translated separately and rejoined."""
    return [s for s in _SENTENCE_RE.split(text) if s.strip()]


class NllbTranslation:
    def __init__(self, engine, from_lang, to_lang):
        self.engine = engine
        self.from_lang = from_lang
        self.to_lang = to_lang

    def translate(self, text):
        return self.translate_many([text])[0]

    def translate_many(self, texts):
        """Translates several independent texts in a single engine call by
        collecting all their sentences together, then rebuilding each text
        from the results."""
        all_sentences = []
        # one layout per text: literal strings kept verbatim (the newline runs
        # that keep a multi-paragraph file's structure, and anything with no
        # sentence in it) and (start, count) slices of all_sentences
        layouts = []

        for text in texts:
            layout = []
            for segment in _PARAGRAPH_RE.split(text):
                sentences = split_sentences(segment)
                if sentences:
                    layout.append((len(all_sentences), len(sentences)))
                    all_sentences.extend(sentences)
                else:
                    layout.append(segment)
            layouts.append(layout)

        if not all_sentences:
            return list(texts)

        translated = self.engine.translate_batch(
            all_sentences, self.from_lang.flores, self.to_lang.flores
        )

        results = []
        for layout in layouts:
            parts = []
            for part in layout:
                if isinstance(part, tuple):
                    start, count = part
                    parts.append(" ".join(translated[start : start + count]))
                else:
                    parts.append(part)
            results.append("".join(parts))
        return results


class NllbLanguage:
    def __init__(self, engine, code, name, flores):
        self.engine = engine
        self.code = code
        self.name = name
        self.flores = flores

    def __str__(self):
        return self.name

    def get_translation(self, to):
        return NllbTranslation(self.engine, self, to)


def get_installed_languages(path=None, threads=None, beam_size=NllbEngine.DEFAULT_BEAM):
    """Same entry point shape as argostranslate.translate: every language
    pair is available, all sharing one lazily-loaded engine."""
    engine = NllbEngine(path, threads, beam_size)
    return [
        NllbLanguage(engine, code, name, flores)
        for code, name, flores in LANGUAGES
    ]
