"""Translation provider seam (DOCUMENTATION.md §6).

Behind a one-method interface so the provider is swappable without touching the
endpoint: Groq (session 4, lowest TTFT), Ollama (zero-cost local fallback), or
this mock. Session 2 uses the mock — no network, no API key, no model — so the
pipeline is provable offline and the translation quality question is deferred.
"""

from __future__ import annotations

from typing import Protocol


class Translator(Protocol):
    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context_lines: list[str],
    ) -> str:
        """Translate only `text`. context_lines are the recent SOURCE lines,
        passed as reference for context-aware decoding (DOCUMENTATION.md 6a/6b);
        a real provider weaves them into the prompt without re-emitting them."""
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
    ) -> str:
        if not text:
            return ""
        ctx = f"·ctx{len(context_lines)}" if context_lines else ""
        return f"[{target_lang}·mock{ctx}] {text}"
