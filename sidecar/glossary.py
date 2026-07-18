"""Consistency glossary — per-session term memory so a recurring term is rendered
the same way every time (fixes the 黄羊-class scatter, DOCUMENTATION.md 6b).

Two layers, both plain JSON so they are human- AND LLM-editable/auditable:
  glossary/universal.json      curated, shareable, GENRE-NEUTRAL terms only
                               (never character names — those collide across shows)
  glossary/shows/<slug>.json   per-show, auto-built from a run's log by an LLM
                               extraction pass; names/places/show-specific terms

At translation time we inject only the entries whose Chinese appears in the
current line (or its context) — usually 0-2 — so the prompt never bloats and a
60-term glossary costs nothing per line. Consistency, not correctness: auto-built
entries pin whatever the model first read; edit the JSON (or the seed) to correct.

CLI (populate a per-show glossary from a run's log):
    python glossary.py build ../logs/cdrama-...-<model>.jsonl
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_DIR = Path(os.getenv("CDT_GLOSSARY_DIR", str(Path(__file__).resolve().parent.parent / "glossary")))
_SHOWS = _DIR / "shows"


def slug(label: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "-", (label or "").strip()).strip("-")[:60]
    return s or "default"


def _flatten(g: dict) -> dict[str, str]:
    """{names:{zh:en}, places:{...}, terms:{...}} or a flat {zh:en} -> flat {zh:en}."""
    out: dict[str, str] = {}
    for k, v in (g or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            out.update({zh: en for zh, en in v.items() if not zh.startswith("_")})
        elif isinstance(v, str):
            out[k] = v
    return out


def _load(path: Path) -> dict[str, str]:
    try:
        return _flatten(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return {}


class Glossary:
    def __init__(self) -> None:
        self._universal = _load(_DIR / "universal.json")
        self._shows: dict[str, dict[str, str]] = {}

    def _show(self, label: str) -> dict[str, str]:
        s = slug(label)
        if s not in self._shows:
            self._shows[s] = _load(_SHOWS / f"{s}.json")
        return self._shows[s]

    def all_terms(self, label: str) -> dict[str, str]:
        """The merged glossary for a show — curated universal wins conflicts.
        Public so the audit can measure per-term adherence across a run."""
        return {**self._show(label), **self._universal}

    def matching(self, text: str, context: list[str] | None, label: str) -> dict[str, str]:
        """Glossary entries whose Chinese key appears in the line or its context.
        Longest keys first so an alias like 彭雄飞 wins over 雄飞."""
        merged = self.all_terms(label)
        hay = (text or "") + " " + " ".join(context or [])
        hits: dict[str, str] = {}
        for zh, en in sorted(merged.items(), key=lambda kv: -len(kv[0])):
            if zh and en and zh in hay:
                # skip a shorter key already covered by a longer matched alias
                if not any(zh in k for k in hits):
                    hits[zh] = en
        return hits


GLOSSARY = Glossary()


# --- population: LLM extraction from a run's log ---------------------------- #

def build_from_log(logfile: str, show: str | None = None) -> None:
    import requests
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        print("build needs GROQ_API_KEY (repo-root .env)", file=sys.stderr)
        return

    rows = [json.loads(l) for l in open(logfile, encoding="utf-8") if l.strip()]
    oks = [r for r in rows if r.get("stage", "translate") == "translate" and r.get("status") == "ok"]
    src = list(dict.fromkeys(r["source_text"] for r in oks if r.get("source_text")))
    label = show or next((r.get("label") for r in oks if r.get("label")), "") or "default"
    if not src:
        print("no source lines in log", file=sys.stderr)
        return

    sess = requests.Session()
    sess.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    model = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
    user = (
        "These are subtitle lines from ONE drama episode. List ONLY terms that must be "
        "translated CONSISTENTLY across the episode and would otherwise scatter: "
        "character/person names, place/org names, and show-specific or domain/technical "
        "terms. IGNORE ordinary words and function words. Give the single best English for "
        'each. JSON shape: {"names":{zh:en},"places":{zh:en},"terms":{zh:en}}.\n\nLINES:\n'
        + "\n".join(src)
    )
    body = {
        "model": model, "temperature": 0, "max_tokens": 2000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "You extract a consistency glossary from Chinese TV-drama subtitles. Output JSON only."},
            {"role": "user", "content": user},
        ],
    }
    if any(k in model.lower() for k in ("qwen", "qwq")):
        body["reasoning_effort"] = "none"
    r = sess.post("https://api.groq.com/openai/v1/chat/completions", json=body, timeout=90)
    r.raise_for_status()
    extracted = json.loads(r.json()["choices"][0]["message"]["content"])

    s = slug(label)
    path = _SHOWS / f"{s}.json"
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    # Merge: keep existing values (preserve hand/LLM corrections); add only new
    # keys; and never shadow a term the curated universal already owns.
    universal = _load(_DIR / "universal.json")
    added = 0
    for cat in ("names", "places", "terms"):
        cur = existing.setdefault(cat, {})
        for zh, en in (extracted.get(cat) or {}).items():
            if zh in universal or zh in cur:
                continue
            cur[zh] = en
            added += 1
    _SHOWS.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(existing.get(c, {})) for c in ("names", "places", "terms"))
    print(f"glossary for '{label}' (slug={s}): +{added} new, {total} total -> {path}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "build":
        build_from_log(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        print(__doc__.strip().splitlines()[-1])
        print("usage: python glossary.py build <logfile> [show-label]")
