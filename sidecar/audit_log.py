"""Append-only JSONL log of every translation, for auditing at scale.

One JSON object per line so it is trivial to tail, grep, `jq`, or load into
pandas — and it survives crashes (no buffering to lose). Each sidecar run writes
its OWN timestamped file (`logs/cdrama-<YYYYMMDD-HHMMSS>.jsonl`), so sessions
never mix. The source hanzi and the rendered translation both cross into the log
(invariant 2 in spirit: never throw the source away), alongside the metadata that
makes stats and accuracy evaluation possible.

Config (env):
  CDT_LOG        "0"/"false" to disable (default on)
  CDT_LOG_DIR    directory for the per-run files (default: <repo>/logs)
  CDT_LOG_PATH   pin ONE explicit file instead of per-run (tests / override)

Logging must never break a translation: every failure here is swallowed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger("sidecar.audit")

LOG_DIR = Path(os.getenv("CDT_LOG_DIR", str(Path(__file__).resolve().parent.parent / "logs")))
ENABLED = os.getenv("CDT_LOG", "1").strip().lower() not in ("0", "false", "no", "off")


# Fixed at import = one file per run. The model tag is filled in by set_run_tag()
# at startup (before any line is logged), so runs self-separate by model.
_STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
_run_tag = ""
_log_path: Path | None = None


def set_run_tag(tag: str) -> None:
    """Tag this run's file with e.g. the model slug, so llama and qwen runs land
    in differently-named files (cdrama-<stamp>-<tag>.jsonl)."""
    global _run_tag, _log_path
    base = (tag or "").split("/")[-1]
    _run_tag = "".join(c if (c.isalnum() or c in "._-") else "-" for c in base)[:40]
    _log_path = None  # re-resolve on next append


def _resolve_path() -> Path:
    # CDT_LOG_PATH pins a single explicit file (used by tests); else per-run.
    explicit = os.getenv("CDT_LOG_PATH", "").strip()
    if explicit:
        return Path(explicit)
    suffix = f"-{_run_tag}" if _run_tag else ""
    return LOG_DIR / f"cdrama-{_STAMP}{suffix}.jsonl"

def append(entry: dict) -> None:
    """Append one entry as a JSON line. No-op if disabled; never raises."""
    if not ENABLED:
        return
    global _log_path
    try:
        if _log_path is None:
            _log_path = _resolve_path()
            _log_path.parent.mkdir(parents=True, exist_ok=True)
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("translation log append failed", exc_info=True)
