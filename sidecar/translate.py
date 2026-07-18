"""Translation provider seam (DOCUMENTATION.md §6).

Behind a one-method interface so the provider is swappable without touching the
endpoint: Groq (hosted, lowest TTFT), Ollama (zero-cost local fallback), or the
mock. make_translator() picks Groq when GROQ_API_KEY is set, else the mock, so
the sidecar always runs — key-free with the mock, real with a key.

Context-aware decoding (6a/6b): recent SOURCE lines are passed as reference so
split-sentence chunks read coherently; the model translates only the current
line and must not re-emit the context.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

log = logging.getLogger("sidecar.translate")


class Translator(Protocol):
    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context_lines: list[str],
        continuation: bool,
    ) -> str:
        """Translate `text`. context_lines are the recent SOURCE lines, passed as
        reference for context-aware decoding (6a/6b). When continuation is true,
        those lines and `text` are one split sentence and the provider returns the
        combined-sentence translation (re-translation / screen-rewriting, 6a)."""
        ...


class MockTranslator:
    """Deterministic stand-in that tags its output, so it is never mistaken for
    real machine translation during testing. It also surfaces how much context
    it received, so the context_lines plumbing is visible end to end."""

    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context_lines: list[str] | None = None,
        continuation: bool = False,
    ) -> str:
        if not text:
            return ""
        ctx = f"·ctx{len(context_lines)}" if context_lines else ""
        cont = "·cont" if continuation else ""
        return f"[{target_lang}·mock{ctx}{cont}] {text}"


# --- Groq provider --------------------------------------------------------- #

_LANG = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "id": "Indonesian",
    "ar": "Arabic", "hi": "Hindi", "fr": "French", "ja": "Japanese",
    "ko": "Korean", "de": "German", "ru": "Russian", "vi": "Vietnamese",
}
_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


def _system_prompt(lang_name: str) -> str:
    # Constant per target language, so it forms a stable prefix Groq can cache.
    # Treats OCR as a noisy channel and the model as a denoising translator
    # (tolerate glitches, don't invent) while preserving dramatic nuance.
    return (
        f"You are translating live subtitles for a Chinese TV drama into "
        f"{lang_name}. The Chinese text comes from OCR of burned-in subtitles and "
        f"may contain occasional character-recognition errors: a visually similar "
        f"hanzi, a stray extra character, a dropped one. Translate the intended "
        f"meaning — silently work around obvious OCR glitches — but do NOT invent "
        f"content: if part of a line is too garbled to read, translate only what "
        f"is clear. Preserve the tone and register of dramatic dialogue and render "
        f"idioms, chengyu and set phrases naturally rather than word-for-word, but "
        f"never flatten a deliberately odd, blunt or ironic line into a bland one. "
        f"Output only the {lang_name} translation itself: no quotes, no pinyin, no "
        f"notes, no source text, no explanations."
    )


class GroqTranslator:
    """Groq chat-completions (OpenAI-compatible), lowest TTFT (§6). Keeps a warm
    HTTP session; on any failure returns "" so the pipeline degrades to 'no
    overlay for this line' rather than throwing."""

    def __init__(self, api_key: str, model: str, timeout: float = 8.0) -> None:
        import requests  # local import so the mock path needs no dependency

        self.model = model
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        self._sys: dict[str, str] = {}

    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context_lines: list[str] | None = None,
        continuation: bool = False,
    ) -> str:
        if not text:
            return ""
        lang_name = _LANG.get(target_lang, target_lang)
        system = self._sys.setdefault(lang_name, _system_prompt(lang_name))
        ctx = list(context_lines or [])
        if continuation and ctx:
            # Re-translation / screen-rewriting (6a) with the model as arbiter of
            # whether to bridge at all: fuse only a genuine same-speaker split
            # sentence; refuse to fuse a dialogue turn between different speakers
            # (that leak is the bug this guards against). The extension flags a
            # *candidate* continuation; the model makes the final semantic call.
            sentence = "\n".join(ctx + [text])
            # Default to SEPARATE: without speaker labels, continuation vs. a
            # two-speaker exchange is genuinely ambiguous, and wrongly fusing a
            # turn is the bad failure; leaving a loose continuation unbridged is
            # mild (still two readable subtitles). So bridge only on unmistakable
            # mid-clause splits.
            user = (
                "Below are consecutive subtitle lines, earliest first. By default "
                "the last line is a NEW speaker's turn — translate ONLY the last "
                f"line into {lang_name}. Merge it with the line(s) above into a "
                "single sentence ONLY if they are unmistakably one sentence by one "
                "speaker split mid-clause (e.g. a 虽然…/如果…/因为… clause finished "
                "by the last line). When in doubt, translate only the last line. "
                f"Output just the {lang_name} translation, no labels.\n{sentence}"
            )
        elif ctx:
            user = (
                "Earlier lines, for context only — do not translate or repeat "
                "them:\n" + "\n".join(ctx) +
                f"\n\nTranslate only this line, as a natural continuation:\n{text}"
            )
        else:
            user = f"Translate this line:\n{text}"
        try:
            r = self._session.post(
                _GROQ_ENDPOINT,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 100,
                    "stream": False,
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"].strip()
            return out.strip('"').strip()
        except Exception as e:  # network, rate limit, bad key/model, parse
            log.warning("groq translate failed (%s): %s", type(e).__name__, e)
            return ""


def make_translator() -> Translator:
    """Groq if GROQ_API_KEY is set (and not the placeholder), else the mock.
    CDT_TRANSLATOR=mock forces the mock even with a key (offline testing)."""
    if os.getenv("CDT_TRANSLATOR", "").strip().lower() == "mock":
        log.info("translator: MockTranslator (forced by CDT_TRANSLATOR=mock)")
        return MockTranslator()
    key = os.getenv("GROQ_API_KEY", "").strip()
    if key and not key.startswith("gsk_your"):
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        log.info("translator: GroqTranslator (model=%s)", model)
        return GroqTranslator(key, model)
    log.info("translator: MockTranslator (no GROQ_API_KEY)")
    return MockTranslator()
