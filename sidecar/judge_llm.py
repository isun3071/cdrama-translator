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

import collections
import os

_GROQ = "https://api.groq.com/openai/v1/chat/completions"
_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

# Running token/cost tally for one judge pass (reset_usage() at the start of each).
# Token counts are REAL (from each response's usage); prices are rough 2026 $/1M and
# meant for order-of-magnitude only — edit as they move, or rely on OpenRouter's
# billed `usage.cost` which is used verbatim when present.
_USAGE = collections.defaultdict(lambda: {"prompt": 0, "completion": 0, "calls": 0, "cost": 0.0})
_PRICE = {  # (input, output) $/1M — SPECIFIC keys first (substring match, first wins)
    "deepseek": (0.14, 0.28), "ling": (0.01, 0.02),
    "gemini-2.5-flash": (0.10, 0.40), "gemini-flash": (0.10, 0.40), "gemini": (0.30, 1.20),
    "gpt-4o-mini": (0.15, 0.60), "gpt-5-mini": (0.25, 2.0), "gpt": (2.5, 10.0),
    "haiku": (0.80, 4.0), "claude": (3.0, 15.0),
    "glm": (0.30, 0.90), "minimax": (0.60, 2.40), "qwen": (0.40, 1.20),
    "llama": (0.0, 0.0), "gemma": (0.0, 0.0),
}


def judge_session(model: str | None = None):
    """Resolve (session, url, model, provider, has_key) for the configured grader.
    `model` overrides the env model (drift judge passes GROQ_DRIFT_MODEL through)."""
    import requests

    provider = os.getenv("JUDGE_PROVIDER", "groq").strip().lower()
    model = (model or os.getenv("JUDGE_MODEL", "").strip() or os.getenv("GROQ_JUDGE_MODEL", "").strip())
    if model.startswith("#"):   # a .env inline comment leaked in as the value — ignore, use default
        model = ""
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
    data = r.json()
    u = data.get("usage") or {}
    rec = _USAGE[model]
    rec["prompt"] += u.get("prompt_tokens", 0) or 0
    rec["completion"] += u.get("completion_tokens", 0) or 0
    rec["cost"] += u.get("cost", 0) or 0        # OpenRouter returns billed cost; Groq doesn't
    rec["calls"] += 1
    # content can be null (a reasoning model that spent its budget thinking, a refusal,
    # a truncation) — coerce to "" so callers get a string, never None.
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return msg.get("content") or ""


def reset_usage() -> None:
    _USAGE.clear()


def _price_for(model: str):
    m = model.lower()
    for key, price in _PRICE.items():
        if key in m:
            return price
    return None


def cost_estimate() -> str:
    """One-line tally for a finished judge pass — real token counts, billed cost when
    the provider returns it (OpenRouter), else a rough estimate from the price table."""
    if not _USAGE:
        return ""
    calls = pin = pout = billed_calls = 0
    est = actual = 0.0
    unpriced = []
    for model, u in _USAGE.items():
        calls += u["calls"]; pin += u["prompt"]; pout += u["completion"]; actual += u["cost"]
        if u["cost"] > 0:
            billed_calls += u["calls"]
        p = _price_for(model)
        if p:
            est += u["prompt"] / 1e6 * p[0] + u["completion"] / 1e6 * p[1]
        elif not u["cost"]:
            unpriced.append(model.split("/")[-1])
    use_billed = actual > 0 and billed_calls == calls   # only trust billed if EVERY call reported it
    money = (f"${actual:.4f} billed" if use_billed
             else f"~${est:.4f} rough est." + (f" ({len(unpriced)} unpriced)" if unpriced else ""))
    return f"cost: {calls} calls · {pin:,} in + {pout:,} out tok · {money}"


def voters(model: str, votes: int):
    """The grader models to poll per line, and the temperature. JUDGE_MODELS
    (comma-separated) => a diverse PANEL: one vote each at temp 0 (independence comes
    from using different models). Otherwise N votes of `model`, at temp 0.4 when N>1
    so the votes actually vary (temp 0 would make them identical)."""
    panel = [m.strip() for m in os.getenv("JUDGE_MODELS", "").split(",") if m.strip()]
    if panel:
        return panel, 0.0
    return [model] * max(1, votes), (0.4 if votes > 1 else 0.0)
