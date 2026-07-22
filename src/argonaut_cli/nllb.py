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

from argonaut.translation import CancelledError

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
    return all(
        os.path.getsize(os.path.join(path, name)) > 0
        if os.path.exists(os.path.join(path, name))
        else False
        for name in MODEL_FILES
    )


def download_model(path=None, base_url=None, on_progress=None, is_cancelled=None):
    """Downloads the model files to `path`, reporting (done_bytes,
    total_bytes) after each chunk. Cancelling removes the partial file."""
    path = path or model_dir()
    base_url = base_url or MODEL_BASE_URL
    on_progress = on_progress or (lambda done, total: None)
    is_cancelled = is_cancelled or (lambda: False)
    os.makedirs(path, exist_ok=True)

    responses = [
        urllib.request.urlopen(f"{base_url}/{name}") for name in MODEL_FILES
    ]
    total = sum(int(r.headers.get("Content-Length") or 0) for r in responses)
    done = 0
    for name, response in zip(MODEL_FILES, responses):
        target = os.path.join(path, name)
        try:
            with open(target, "wb") as out:
                while True:
                    if is_cancelled():
                        raise CancelledError()
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    on_progress(done, total)
        except BaseException:
            if os.path.exists(target):
                os.remove(target)
            raise
        finally:
            response.close()


def remove_model(path=None):
    """Deletes the downloaded model directory to free disk space."""
    path = path or model_dir()
    if os.path.isdir(path):
        shutil.rmtree(path)


class NllbEngine:
    """Shared CTranslate2 translator, loaded lazily on first use."""

    def __init__(self, path=None, threads=None):
        self.path = path or model_dir()
        self.threads = threads
        self._translator = None
        self._sp = None

    def _load(self):
        if self._translator is None:
            import ctranslate2
            import sentencepiece

            self._translator = ctranslate2.Translator(
                self.path,
                device="cpu",
                intra_threads=self.threads or os.cpu_count() or 4,
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
            beam_size=2,
            max_batch_size=1024,
            batch_type="tokens",
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
        # newlines are kept verbatim so multi-paragraph texts (e.g. whole
        # .txt files) keep their structure
        segments = _PARAGRAPH_RE.split(text)
        translated = []
        for segment in segments:
            sentences = split_sentences(segment)
            if not sentences or segment.startswith("\n"):
                translated.append(segment)
            else:
                translated.append(
                    " ".join(
                        self.engine.translate_batch(
                            sentences, self.from_lang.flores, self.to_lang.flores
                        )
                    )
                )
        return "".join(translated)


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


def get_installed_languages(path=None, threads=None):
    """Same entry point shape as argostranslate.translate: every language
    pair is available, all sharing one lazily-loaded engine."""
    engine = NllbEngine(path, threads)
    return [
        NllbLanguage(engine, code, name, flores)
        for code, name, flores in LANGUAGES
    ]
