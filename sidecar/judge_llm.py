"""Provider routing for the OFFLINE judge/grader calls — NOT the hot-path translator.

The translator stays on Groq for TTFT (it's on the wire, per line). Judging is a
batch pass over the logs, so it can use a stronger, slower, pricier grader — and a
grader with strong CHINESE, since MQM adequacy scoring means reading the source, not
just the English. llama-3.3-70b is a fine fluency judge but weak on Chinese source
fidelity; DeepSeek / Claude / GPT-class read the hanzi properly. OpenRouter gives all
of them through one OpenAI-compatible key.

Env:
  JUDGE_PROVIDER      groq (default) | openrouter
  JUDGE_MODEL         grader model (fallback: GROQ_JUDGE_MODEL); with openrouter use a
                      namespaced id, e.g. deepseek/deepseek-chat, anthropic/claude-sonnet-4
  OPENROUTER_API_KEY  key for JUDGE_PROVIDER=openrouter
  GROQ_API_KEY        key for JUDGE_PROVIDER=groq

Every judge/grader call in the repo (audit.py, drift_judge.py, persona_ab.py) routes
through here, so switching the grader is one env change and never touches translation.
"""

from __future__ import annotations

import os

_GROQ = "https://api.groq.com/openai/v1/chat/completions"
_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


def judge_session(model: str | None = None):
    """Resolve (session, url, model, provider, has_key) for the configured grader.
    `model` overrides the env model (drift judge passes GROQ_DRIFT_MODEL through)."""
    import requests

    provider = os.getenv("JUDGE_PROVIDER", "groq").strip().lower()
    model = (model or os.getenv("JUDGE_MODEL", "").strip() or os.getenv("GROQ_JUDGE_MODEL", "").strip())
    if provider == "openrouter":
        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        url = _OPENROUTER
        model = model or "deepseek/deepseek-chat"
    else:
        provider = "groq"
        key = os.getenv("GROQ_API_KEY", "").strip()
        url = _GROQ
        model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    sess = requests.Session()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "openrouter":
        # Optional but OpenRouter-recommended attribution headers.
        headers["HTTP-Referer"] = "https://github.com/cdrama-translator"
        headers["X-Title"] = "cdrama-translator audit"
    sess.headers.update(headers)
    return sess, url, model, provider, bool(key)


def complete(session, url: str, model: str, messages: list, *, provider: str = "groq",
             response_format: dict | None = None, max_tokens: int = 400,
             temperature: float = 0.0, timeout: float = 40) -> str:
    """One chat-completion against the resolved grader. Returns the message content
    string; raises on transport/HTTP failure. Same OpenAI shape for both providers."""
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if response_format:
        body["response_format"] = response_format
    # reasoning_effort is a Groq-side knob for Qwen/QwQ thinking models; don't send it
    # to OpenRouter (param semantics differ per model there).
    if provider == "groq" and any(k in model.lower() for k in ("qwen", "qwq")):
        body["reasoning_effort"] = "none"
    r = session.post(url, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
