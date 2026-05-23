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
    DEFAULT_MODEL = "qwen2.5:7b"
    BATCH_SIZE = 15  # Segments per batch

    def __init__(self, base_url=None, model=None):
        self.base_url = base_url or os.environ.get("OLLAMA_URL", self.DEFAULT_URL)
        self.model = model or os.environ.get("OLLAMA_MODEL", self.DEFAULT_MODEL)

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
    else:
        return OllamaTranslator(
            base_url=kwargs.get("base_url"),
            model=kwargs.get("model"),
        )
