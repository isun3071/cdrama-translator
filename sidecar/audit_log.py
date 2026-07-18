"""Append-only JSONL log of every translation, for auditing at scale.

One JSON object per line (`translations.jsonl`) so it is trivial to tail, grep,
`jq`, or load into pandas — and it survives crashes (no buffering to lose). The
source hanzi and the rendered translation both cross into the log (invariant 2 in
spirit: never throw the source away), alongside the metadata that makes stats and
accuracy evaluation possible.

Config (env):
  CDT_LOG        "0"/"false" to disable (default on)
  CDT_LOG_PATH   override the file path (default: <repo>/logs/translations.jsonl)

Logging must never break a translation: every failure here is swallowed.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("sidecar.audit")

_DEFAULT = Path(__file__).resolve().parent.parent / "logs" / "translations.jsonl"
LOG_PATH = Path(os.getenv("CDT_LOG_PATH", str(_DEFAULT)))
ENABLED = os.getenv("CDT_LOG", "1").strip().lower() not in ("0", "false", "no", "off")

_dir_ready = False


def append(entry: dict) -> None:
    """Append one entry as a JSON line. No-op if disabled; never raises."""
    if not ENABLED:
        return
    global _dir_ready
    try:
        if not _dir_ready:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _dir_ready = True
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("translation log append failed", exc_info=True)
