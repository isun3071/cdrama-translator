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
        glossary: dict[str, str] | None = None,
        context_note: str = "",
    ) -> str:
        """Translate `text`. context_lines are the recent SOURCE lines, passed as
        reference for context-aware decoding (6a/6b). When continuation is true,
        those lines and `text` are one split sentence and the provider returns the
        combined-sentence translation (re-translation / screen-rewriting, 6a).
        context_note is optional user-supplied show/episode background — reference
        only, never translated or echoed."""
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
        glossary: dict[str, str] | None = None,
        context_note: str = "",
    ) -> str:
        if not text:
            return ""
        ctx = f"·ctx{len(context_lines)}" if context_lines else ""
        cont = "·cont" if continuation else ""
        gl = f"·gl{len(glossary)}" if glossary else ""
        nt = "·note" if context_note else ""
        return f"[{target_lang}·mock{ctx}{cont}{gl}{nt}] {text}"


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
        f"never flatten a deliberately odd, blunt or ironic line into a bland one.\n"
        f"Also:\n"
        f"- When a speaker tags their own name into a first-person clause "
        f"(我张三…, 我傅正川…), drop the name — {lang_name} carries that emphasis "
        f"through intonation or first-person/possessive framing "
        f'(这是我傅正川下的命令 → "this is my order", not "…, Fu Zhengchuan").\n'
        f"- A genuine question word (什么, 谁, 哪, 怎么, 为什么, 几, 多少) makes the "
        f"line a question even with no final 吗 — render it as a real question with "
        f'a question mark (藏了什么好吃的 → "Hiding something good to eat?"); but '
        f"leave non-question uses alone (什么都 = anything, 谁都 = everyone, 几 = a few).\n"
        f"- Never carry a proper noun (name or place) from the context lines into "
        f"the current line unless it also appears in the current line's own source; "
        f'context disambiguates, it is not content (雄飞 only in context → never '
        f'append "…, Xiongfei").\n'
        f"Output only the {lang_name} translation itself — a properly capitalized, "
        f"properly punctuated sentence or clause, even for a continuation fragment "
        f"— with no quotes, pinyin, notes, source text or explanations."
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
        # Qwen/QwQ on Groq default to emitting <think> reasoning traces, which
        # wreck a real-time translator (extra latency + the answer gets truncated
        # past max_tokens). Switch thinking off. Override with GROQ_REASONING_EFFORT.
        effort = os.getenv("GROQ_REASONING_EFFORT", "").strip()
        if not effort and any(k in model.lower() for k in ("qwen", "qwq")):
            effort = "none"
        self.reasoning_effort = effort

    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context_lines: list[str] | None = None,
        continuation: bool = False,
        glossary: dict[str, str] | None = None,
        context_note: str = "",
    ) -> str:
        if not text:
            return ""
        lang_name = _LANG.get(target_lang, target_lang)
        system = self._sys.setdefault(lang_name, _system_prompt(lang_name))
        if context_note:
            # Session-static background -> append to the SYSTEM prompt so it lands in
            # the cacheable prefix and is prefilled ONCE per session (not re-billed per
            # line). The stable base prompt still caches ahead of it; only the note
            # re-prefills if the user edits it. Reference only; never emitted.
            system = (
                system + "\n\nBackground about the show/episode you are subtitling "
                "(reference only — use it to disambiguate names, register and references; "
                "never translate it or copy its wording into your output):\n"
                + context_note.strip()
            )
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
                "\n\nTranslate only this line as its own properly capitalized "
                f"clause (use the context only to disambiguate):\n{text}"
            )
        else:
            user = f"Translate this line:\n{text}"
        if glossary:
            # Pin recurring terms to one rendering (glossary, 6b). Only the terms
            # present in this line are passed in, so this stays 0-2 items.
            pins = "; ".join(f"{zh} = {en}" for zh, en in glossary.items())
            user = f"Use these fixed translations verbatim wherever the term appears: {pins}.\n" + user
        # (context_note is injected into the SYSTEM prompt above — the cacheable
        # prefix — not here, so a long note is prefilled once per session.)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": 100,
            "stream": False,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        try:
            r = self._session.post(_GROQ_ENDPOINT, json=body, timeout=self.timeout)
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"]
            # Defensive: drop any reasoning trace a thinking model still emits.
            if "</think>" in out:
                out = out.split("</think>")[-1]
            elif out.lstrip().startswith("<think>"):
                return ""  # thinking-only (e.g. truncated) — no usable answer
            return out.strip().strip('"').strip()
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
        model = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b").strip()
        log.info("translator: GroqTranslator (model=%s)", model)
        return GroqTranslator(key, model)
    log.info("translator: MockTranslator (no GROQ_API_KEY)")
    return MockTranslator()
