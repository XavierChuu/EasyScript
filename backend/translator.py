"""
Translation engine — supports Ollama (local) and Claude API (cloud).
Translates transcribed speech segments to target languages.
"""

import os
import json
import re
import httpx


# ── Ollama Provider ──

class OllamaTranslator:
    """Local translation using Ollama API."""

    DEFAULT_URL = "http://localhost:11434"
    DEFAULT_MODEL = "qwen2.5:3b"
    BATCH_SIZE = 15  # Segments per batch

    # Live/streaming tuning: short prompt, keep model resident, cap tokens
    STREAM_KEEP_ALIVE = "10m"
    STREAM_OPTIONS = {
        "temperature": 0.2,
        "num_predict": 256,
        "top_p": 0.9,
        "repeat_penalty": 1.05,
    }

    def __init__(self, base_url=None, model=None):
        self.base_url = base_url or os.environ.get("OLLAMA_URL", self.DEFAULT_URL)
        self.model = model or os.environ.get("OLLAMA_MODEL", self.DEFAULT_MODEL)

    # ── Live streaming API ──

    def stream_translate_one(self, text, source_lang, target_lang):
        """Yield response tokens as Ollama streams them.

        Uses a minimal prompt (less prefill = faster first token) and keeps the
        model warm in RAM/VRAM via keep_alive.
        """
        tgt = _lang_name(target_lang)
        prompt = (
            f"Translate to {tgt}. Output ONLY the translation, nothing else.\n"
            f"Text: {text}\n"
            f"Translation:"
        )
        with httpx.stream(
            "POST",
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": True,
                "keep_alive": self.STREAM_KEEP_ALIVE,
                "options": self.STREAM_OPTIONS,
            },
            timeout=60,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                tok = obj.get("response", "")
                if tok:
                    yield tok
                if obj.get("done"):
                    break

    def warmup(self):
        """Pre-load model into RAM/VRAM. Eliminates first-call cold start."""
        try:
            httpx.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": "hi",
                    "stream": False,
                    "keep_alive": self.STREAM_KEEP_ALIVE,
                    "options": {"num_predict": 1, "temperature": 0},
                },
                timeout=120,
            )
            return True
        except Exception as e:
            print(f"[ollama] warmup failed: {e}")
            return False

    def translate(self, segments, source_lang, target_lang,
                  on_progress=None, on_batch_done=None):
        """
        Translate segments in batches.

        Args:
            segments: list of { text: str, ... }
            source_lang: source language code (e.g. "vi", "en")
            target_lang: target language code
            on_progress: callback(float) for progress 0..1
            on_batch_done: callback(results_so_far, batch_end) called after each batch

        Returns: list of { text: str } (translated)
        """
        results = []
        total = len(segments)

        for batch_start in range(0, total, self.BATCH_SIZE):
            batch_end = min(batch_start + self.BATCH_SIZE, total)
            batch = segments[batch_start:batch_end]

            translated = self._translate_batch(batch, source_lang, target_lang)
            results.extend(translated)

            if on_progress:
                on_progress(min(batch_end / total, 0.99))

            if on_batch_done:
                on_batch_done(list(results), batch_end)

        if on_progress:
            on_progress(1.0)

        return results

    def _translate_batch(self, batch, source_lang, target_lang):
        """Translate a batch of segments using numbered lines."""
        # Build numbered input
        lines = []
        for i, seg in enumerate(batch):
            text = seg.get("text", "").strip()
            if text:
                lines.append(f"{i + 1}. {text}")
            else:
                lines.append(f"{i + 1}. [EMPTY]")

        numbered_text = "\n".join(lines)

        prompt = (
            f"Translate the following numbered lines from {_lang_name(source_lang)} "
            f"to {_lang_name(target_lang)}.\n"
            f"Return ONLY the translated lines with the same numbering.\n"
            f"Keep the number prefix exactly as-is. Do not add explanations.\n"
            f"If a line says [EMPTY], keep it as [EMPTY].\n\n"
            f"{numbered_text}"
        )

        try:
            response = httpx.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.3},
                },
                timeout=120,
            )
            response.raise_for_status()
            result_text = response.json().get("response", "")
            return _parse_numbered_response(result_text, len(batch))

        except httpx.ConnectError:
            raise ConnectionError(
                f"Cannot connect to Ollama at {self.base_url}. "
                "Is Ollama running? Start with: ollama serve"
            )
        except Exception as e:
            raise RuntimeError(f"Ollama translation error: {e}")

    @staticmethod
    def check_available(base_url=None):
        """Check if Ollama is running and reachable."""
        url = base_url or OllamaTranslator.DEFAULT_URL
        try:
            r = httpx.get(f"{url}/api/tags", timeout=5)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            return {"available": True, "models": models}
        except Exception:
            return {"available": False, "models": []}


# ── Hy-MT2 Local Provider ──

class HyMT2Translator:
    """Offline translation using Tencent Hy-MT2 (tencent/Hy-MT2-1.8B or 7B)."""

    MODELS = {
        "1.8B": "tencent/Hy-MT2-1.8B",
        "7B": "tencent/Hy-MT2-7B",
    }
    CACHE_DIR = os.path.join(os.path.expanduser("~"), ".easyscript", "models", "hymt2")

    def __init__(self, model_size="1.8B"):
        self.model_size = model_size if model_size in self.MODELS else "1.8B"
        self.model_id = self.MODELS[self.model_size]
        self._model = None
        self._tokenizer = None
        self._device = "cpu"

    def _ensure_loaded(self):
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError:
            raise RuntimeError(
                "transformers and torch are not installed. "
                "Run: pip install transformers torch"
            )

        # The bundle sets OMP_NUM_THREADS=1; CPU generation is far slower that way.
        __import__("torch").set_num_threads(max(1, min(8, os.cpu_count() or 4)))
        self._device = "cuda" if __import__("torch").cuda.is_available() else "cpu"
        dtype = __import__("torch").float16 if self._device == "cuda" else __import__("torch").float32

        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_id,
            cache_dir=self.CACHE_DIR,
            trust_remote_code=True,
        )
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_id,
            cache_dir=self.CACHE_DIR,
            trust_remote_code=True,
            torch_dtype=dtype,
        ).to(self._device)
        self._model.eval()

    def _translate_one(self, text, source_lang, target_lang):
        import torch
        src = _lang_name(source_lang)
        tgt = _lang_name(target_lang)
        # Hy-MT2 chat-style prompt (instruction-following format)
        messages = [
            {"role": "system", "content": f"You are a professional translator. Translate the following text from {src} to {tgt}. Output only the translation, no explanations."},
            {"role": "user", "content": text},
        ]
        # Use apply_chat_template if available, otherwise build manually
        if hasattr(self._tokenizer, "apply_chat_template"):
            prompt = self._tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        else:
            prompt = f"Translate from {src} to {tgt}:\n{text}\nTranslation:"

        inputs = self._tokenizer(prompt, return_tensors="pt").to(self._device)
        with torch.no_grad():
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=512,
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        new_tokens = outputs[0][inputs["input_ids"].shape[1]:]
        return self._tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

    def translate(self, segments, source_lang, target_lang,
                  on_progress=None, on_batch_done=None):
        self._ensure_loaded()
        results = []
        total = len(segments)

        for i, seg in enumerate(segments):
            text = seg.get("text", "").strip()
            if text:
                translated = self._translate_one(text, source_lang, target_lang)
                results.append({"text": translated})
            else:
                results.append({"text": ""})

            if on_progress:
                on_progress((i + 1) / total)
            if on_batch_done:
                on_batch_done(list(results), i + 1)

        return results

    @classmethod
    def is_downloaded(cls, model_size="1.8B"):
        """Check if model weights exist in local cache."""
        import glob
        model_id = cls.MODELS.get(model_size, cls.MODELS["1.8B"])
        folder = "models--" + model_id.replace("/", "--")
        pattern = os.path.join(cls.CACHE_DIR, folder, "**", "*.safetensors")
        return bool(glob.glob(pattern, recursive=True))

    @classmethod
    def download(cls, model_size="1.8B"):
        """Download model from HuggingFace to local cache."""
        from huggingface_hub import snapshot_download
        model_id = cls.MODELS.get(model_size, cls.MODELS["1.8B"])
        os.makedirs(cls.CACHE_DIR, exist_ok=True)
        snapshot_download(
            repo_id=model_id,
            cache_dir=cls.CACHE_DIR,
            ignore_patterns=["*.bin"],  # prefer safetensors
        )


# ── NLLB-200 Local Provider ──

# Map ISO 639-1 (UI/whisper) → NLLB BCP-47-like code (script-tagged).
# NLLB uses these to set source language and force target generation token.
NLLB_LANG_CODES = {
    "vi": "vie_Latn", "en": "eng_Latn", "zh": "zho_Hans",
    "ja": "jpn_Jpan", "ko": "kor_Hang", "fr": "fra_Latn",
    "de": "deu_Latn", "es": "spa_Latn", "pt": "por_Latn",
    "ru": "rus_Cyrl", "th": "tha_Thai", "id": "ind_Latn",
    "ar": "arb_Arab", "hi": "hin_Deva", "bn": "ben_Beng",
    "ms": "zsm_Latn", "tl": "tgl_Latn", "my": "mya_Mymr",
    "km": "khm_Khmr", "lo": "lao_Laoo", "it": "ita_Latn",
    "nl": "nld_Latn", "pl": "pol_Latn", "uk": "ukr_Cyrl",
    "cs": "ces_Latn", "sv": "swe_Latn", "da": "dan_Latn",
    "fi": "fin_Latn", "no": "nob_Latn", "el": "ell_Grek",
    "tr": "tur_Latn", "he": "heb_Hebr", "fa": "pes_Arab",
    "hu": "hun_Latn", "ro": "ron_Latn", "bg": "bul_Cyrl",
    "hr": "hrv_Latn", "sk": "slk_Latn", "sl": "slv_Latn",
    "lt": "lit_Latn", "lv": "lvs_Latn", "et": "est_Latn",
    "ca": "cat_Latn", "gl": "glg_Latn", "eu": "eus_Latn",
    "af": "afr_Latn", "sw": "swh_Latn", "ta": "tam_Taml",
    "te": "tel_Telu", "ur": "urd_Arab", "ne": "npi_Deva",
    "si": "sin_Sinh", "ka": "kat_Geor", "az": "azj_Latn",
    "uz": "uzn_Latn", "kk": "kaz_Cyrl", "mn": "khk_Cyrl",
}


def _resolve_nllb_lang(code, fallback="eng_Latn"):
    """Map ISO code → NLLB code. 'auto' falls back to provided default."""
    if not code or code == "auto":
        return fallback
    return NLLB_LANG_CODES.get(code, fallback)


class NLLBTranslator:
    """Offline translation using Meta's NLLB-200 (No Language Left Behind).

    Runs on CTranslate2 — the engine Whisper already uses — with the model
    converted once to int8 next to the download: batched beam search on CUDA
    is ~48 ms per sentence on an RTX 3060 (~170x the old transformers-on-CPU
    path, same output), and ~0.7 s per sentence on a CPU. The transformers
    path remains as a fallback if CTranslate2 can't load the model.
    """

    MODELS = {
        "600M": "facebook/nllb-200-distilled-600M",
        "1.3B": "facebook/nllb-200-distilled-1.3B",
    }
    CACHE_DIR = os.path.join(os.path.expanduser("~"), ".easyscript", "models", "nllb")
    CT2_DIR = os.path.join(os.path.expanduser("~"), ".easyscript", "models", "nllb-ct2")
    BATCH = 32          # sentences per translate_batch call (progress granularity)
    BEAM = 4
    MAX_TOKENS = 256

    def __init__(self, model_size="600M"):
        self.model_size = model_size if model_size in self.MODELS else "600M"
        self.model_id = self.MODELS[self.model_size]
        self._model = None        # transformers fallback
        self._ct2 = None          # ctranslate2.Translator
        self._tokenizer = None
        self._device = "cpu"

    # ── Loading ──

    def _snapshot(self):
        from huggingface_hub import snapshot_download
        try:
            return snapshot_download(self.model_id, cache_dir=self.CACHE_DIR, local_files_only=True)
        except Exception:
            os.makedirs(self.CACHE_DIR, exist_ok=True)
            return snapshot_download(self.model_id, cache_dir=self.CACHE_DIR)

    def _load_ct2(self, snapshot):
        import ctranslate2
        out = os.path.join(self.CT2_DIR, self.model_size)
        if not os.path.isfile(os.path.join(out, "model.bin")):
            # One-time conversion (~10 s for 600M); written aside, then moved
            # into place so an interrupted run never leaves a broken model.
            import shutil
            tmp = out + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(self.CT2_DIR, exist_ok=True)
            ctranslate2.converters.TransformersConverter(snapshot).convert(tmp, quantization="int8")
            shutil.rmtree(out, ignore_errors=True)
            os.replace(tmp, out)
        if ctranslate2.get_cuda_device_count() > 0:
            try:
                self._ct2 = ctranslate2.Translator(out, device="cuda", compute_type="int8_float16")
                self._device = "cuda (CTranslate2)"
                return
            except Exception as e:
                print(f"[nllb] CUDA unavailable for CTranslate2 ({e}); using CPU")
        self._ct2 = ctranslate2.Translator(out, device="cpu", compute_type="int8",
                                           intra_threads=max(1, min(8, os.cpu_count() or 4)))
        self._device = "cpu (CTranslate2)"

    def _ensure_loaded(self):
        if self._ct2 is not None or self._model is not None:
            return
        try:
            from transformers import AutoTokenizer
        except ImportError:
            raise RuntimeError("transformers is not installed. Run: pip install transformers")
        snapshot = self._snapshot()
        self._tokenizer = AutoTokenizer.from_pretrained(snapshot)
        try:
            self._load_ct2(snapshot)
            return
        except Exception as e:
            print(f"[nllb] CTranslate2 path failed ({e}); falling back to transformers")
            self._ct2 = None

        import torch
        from transformers import AutoModelForSeq2SeqLM
        # The bundle sets OMP_NUM_THREADS=1; generation is ~6x slower that way.
        torch.set_num_threads(max(1, min(8, os.cpu_count() or 4)))
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if self._device == "cuda" else torch.float32
        self._model = AutoModelForSeq2SeqLM.from_pretrained(snapshot, dtype=dtype).to(self._device)
        self._model.eval()

    def _auto_src_for_target(self, target_lang):
        """When the caller passes source='auto', pick a sensible default
        based on the target (vi target → en source, otherwise vi)."""
        if target_lang == "vi":
            return "eng_Latn"
        return "vie_Latn"

    def _langs(self, source_lang, target_lang):
        src = _resolve_nllb_lang(source_lang, fallback=self._auto_src_for_target(target_lang))
        tgt = _resolve_nllb_lang(target_lang, fallback="eng_Latn")
        return src, tgt

    def _source_tokens(self, texts, src):
        self._tokenizer.src_lang = src
        return [self._tokenizer.convert_ids_to_tokens(self._tokenizer.encode(t)) for t in texts]

    def _decode(self, tokens):
        # Hypotheses start with the target-language token.
        ids = self._tokenizer.convert_tokens_to_ids(tokens[1:] if tokens else [])
        return self._tokenizer.decode(ids, skip_special_tokens=True).strip()

    def _translate_many(self, texts, source_lang, target_lang, num_beams=BEAM):
        src, tgt = self._langs(source_lang, target_lang)
        if self._ct2 is not None:
            res = self._ct2.translate_batch(
                self._source_tokens(texts, src), target_prefix=[[tgt]] * len(texts),
                beam_size=num_beams, max_batch_size=self.BATCH,
                max_decoding_length=self.MAX_TOKENS)
            return [self._decode(r.hypotheses[0]) for r in res]
        return [self._translate_hf(t, src, tgt, num_beams) for t in texts]

    def _translate_hf(self, text, src, tgt, num_beams):
        import torch
        self._tokenizer.src_lang = src
        inputs = self._tokenizer(text, return_tensors="pt").to(self._device)
        forced_bos = self._tokenizer.convert_tokens_to_ids(tgt)
        with torch.no_grad():
            outputs = self._model.generate(**inputs, forced_bos_token_id=forced_bos,
                                           max_new_tokens=self.MAX_TOKENS, num_beams=num_beams)
        return self._tokenizer.batch_decode(outputs, skip_special_tokens=True)[0].strip()

    def _translate_one(self, text, source_lang, target_lang, num_beams=BEAM):
        return self._translate_many([text], source_lang, target_lang, num_beams)[0]

    # ── Live streaming API (token-level) ──

    def stream_translate_one(self, text, source_lang, target_lang):
        """Yield translation chunks as tokens are decoded (greedy)."""
        self._ensure_loaded()
        src, tgt = self._langs(source_lang, target_lang)
        if self._ct2 is not None:
            tokens, emitted = [], ""
            for step in self._ct2.generate_tokens(
                    self._source_tokens([text], src)[0], target_prefix=[tgt],
                    max_decoding_length=self.MAX_TOKENS):
                if step.is_last:
                    break
                tokens.append(step.token)
                now = self._tokenizer.decode(self._tokenizer.convert_tokens_to_ids(tokens),
                                             skip_special_tokens=True)
                if len(now) > len(emitted) and now.startswith(emitted):
                    yield now[len(emitted):]
                    emitted = now
            return

        from transformers import TextIteratorStreamer
        from threading import Thread
        self._tokenizer.src_lang = src
        inputs = self._tokenizer(text, return_tensors="pt").to(self._device)
        streamer = TextIteratorStreamer(self._tokenizer, skip_prompt=True, skip_special_tokens=True)
        gen_kwargs = dict(**inputs, forced_bos_token_id=self._tokenizer.convert_tokens_to_ids(tgt),
                          max_new_tokens=self.MAX_TOKENS, num_beams=1, do_sample=False, streamer=streamer)
        thread = Thread(target=self._model.generate, kwargs=gen_kwargs)
        thread.start()
        try:
            for chunk in streamer:
                if chunk:
                    yield chunk
        finally:
            thread.join()

    def warmup(self):
        """Pre-load model + run a tiny translate so first real call isn't cold."""
        try:
            self._ensure_loaded()
            self._translate_one("hello", "en", "vi", num_beams=1)
            return True
        except Exception as e:
            print(f"[nllb] warmup failed: {e}")
            return False

    def translate(self, segments, source_lang, target_lang,
                  on_progress=None, on_batch_done=None):
        self._ensure_loaded()
        total = len(segments)
        results = [{"text": ""} for _ in segments]
        todo = [(i, (s.get("text") or "").strip()) for i, s in enumerate(segments)]
        todo = [(i, t) for i, t in todo if t]
        done = total - len(todo)
        for k in range(0, len(todo), self.BATCH):
            chunk = todo[k:k + self.BATCH]
            for (i, _), out in zip(chunk, self._translate_many([t for _, t in chunk], source_lang, target_lang)):
                results[i] = {"text": out}
            done += len(chunk)
            if on_progress:
                on_progress(done / max(1, total))
            if on_batch_done:
                on_batch_done(list(results), done)
        if on_progress:
            on_progress(1.0)
        return results

    @classmethod
    def is_downloaded(cls, model_size="600M"):
        """Check if model weights exist in local cache."""
        import glob
        model_id = cls.MODELS.get(model_size, cls.MODELS["600M"])
        folder = "models--" + model_id.replace("/", "--")
        for ext in ("*.safetensors", "*.bin"):
            if glob.glob(os.path.join(cls.CACHE_DIR, folder, "**", ext), recursive=True):
                return True
        return False

    @classmethod
    def download(cls, model_size="600M"):
        """Download model from HuggingFace to local cache."""
        from huggingface_hub import snapshot_download
        model_id = cls.MODELS.get(model_size, cls.MODELS["600M"])
        os.makedirs(cls.CACHE_DIR, exist_ok=True)
        # The NLLB repos ship pytorch_model.bin (no safetensors) — keep it.
        snapshot_download(
            repo_id=model_id,
            cache_dir=cls.CACHE_DIR,
            allow_patterns=["*.json", "*.model", "*.bin", "*.safetensors", "*.txt"],
        )


# ── Claude API Provider ──

class ClaudeTranslator:
    """Cloud translation using Anthropic Claude API."""

    DEFAULT_MODEL = "claude-sonnet-4-20250514"
    BATCH_SIZE = 25  # Claude handles larger batches well

    def __init__(self, api_key=None, model=None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or self.DEFAULT_MODEL

    def translate(self, segments, source_lang, target_lang,
                  on_progress=None, on_batch_done=None):
        """Translate segments in batches using Claude API."""
        if not self.api_key:
            raise ValueError(
                "Anthropic API key required. Set ANTHROPIC_API_KEY "
                "environment variable or configure in settings."
            )

        import anthropic
        self.client = anthropic.Anthropic(api_key=self.api_key)

        results = []
        total = len(segments)

        for batch_start in range(0, total, self.BATCH_SIZE):
            batch_end = min(batch_start + self.BATCH_SIZE, total)
            batch = segments[batch_start:batch_end]

            translated = self._translate_batch(batch, source_lang, target_lang)
            results.extend(translated)

            if on_progress:
                on_progress(min(batch_end / total, 0.99))

            if on_batch_done:
                on_batch_done(list(results), batch_end)

        if on_progress:
            on_progress(1.0)

        return results

    def _translate_batch(self, batch, source_lang, target_lang):
        """Translate a batch using Claude API."""
        lines = []
        for i, seg in enumerate(batch):
            text = seg.get("text", "").strip()
            if text:
                lines.append(f"{i + 1}. {text}")
            else:
                lines.append(f"{i + 1}. [EMPTY]")

        numbered_text = "\n".join(lines)

        system_prompt = (
            f"You are a professional translator. Translate text from "
            f"{_lang_name(source_lang)} to {_lang_name(target_lang)}. "
            f"Maintain the original meaning, tone, and style. "
            f"For podcast/speech content, keep it natural and conversational."
        )

        user_prompt = (
            f"Translate these numbered lines. Return ONLY the translations "
            f"with the same numbering. No explanations.\n"
            f"If a line says [EMPTY], keep it as [EMPTY].\n\n"
            f"{numbered_text}"
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            result_text = response.content[0].text
            return _parse_numbered_response(result_text, len(batch))

        except Exception as e:
            raise RuntimeError(f"Claude API error: {e}")


# ── Helpers ──

LANG_NAMES = {
    "vi": "Vietnamese", "en": "English", "zh": "Chinese",
    "ja": "Japanese", "ko": "Korean", "fr": "French",
    "de": "German", "es": "Spanish", "pt": "Portuguese",
    "ru": "Russian", "th": "Thai", "id": "Indonesian",
    "ar": "Arabic", "hi": "Hindi", "bn": "Bengali",
    "ms": "Malay", "tl": "Filipino", "my": "Burmese",
    "km": "Khmer", "lo": "Lao", "it": "Italian",
    "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian",
    "cs": "Czech", "sv": "Swedish", "da": "Danish",
    "fi": "Finnish", "no": "Norwegian", "el": "Greek",
    "tr": "Turkish", "he": "Hebrew", "fa": "Persian",
    "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian",
    "hr": "Croatian", "sk": "Slovak", "sl": "Slovenian",
    "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian",
    "ca": "Catalan", "gl": "Galician", "eu": "Basque",
    "af": "Afrikaans", "sw": "Swahili", "ta": "Tamil",
    "te": "Telugu", "ur": "Urdu", "ne": "Nepali",
    "si": "Sinhala", "ka": "Georgian", "az": "Azerbaijani",
    "uz": "Uzbek", "kk": "Kazakh", "mn": "Mongolian",
}


def _lang_name(code):
    """Get full language name from code."""
    return LANG_NAMES.get(code, code)


def _parse_numbered_response(text, expected_count):
    """
    Parse numbered response lines back into a list of { text: str }.
    Handles edge cases: missing numbers, extra whitespace, etc.
    """
    results = [{"text": ""}] * expected_count

    # Try to parse numbered lines: "1. translated text"
    pattern = re.compile(r"^(\d+)\.\s*(.*)$", re.MULTILINE)
    matches = pattern.findall(text)

    for num_str, translated in matches:
        idx = int(num_str) - 1
        if 0 <= idx < expected_count:
            cleaned = translated.strip()
            if cleaned == "[EMPTY]":
                cleaned = ""
            results[idx] = {"text": cleaned}

    # Fallback: if regex found nothing, split by newlines
    if not matches:
        lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if i < expected_count:
                # Remove leading number if present
                cleaned = re.sub(r"^\d+[\.\)]\s*", "", line).strip()
                if cleaned == "[EMPTY]":
                    cleaned = ""
                results[i] = {"text": cleaned}

    return results


def get_translator(provider="ollama", **kwargs):
    """Factory function to get the appropriate translator."""
    if provider == "claude":
        return ClaudeTranslator(
            api_key=kwargs.get("api_key"),
            model=kwargs.get("model"),
        )
    elif provider == "hymt2":
        return HyMT2Translator(
            model_size=kwargs.get("model_size", "1.8B"),
        )
    elif provider == "nllb":
        return NLLBTranslator(
            model_size=kwargs.get("model_size", "600M"),
        )
    else:
        return OllamaTranslator(
            base_url=kwargs.get("base_url"),
            model=kwargs.get("model"),
        )
