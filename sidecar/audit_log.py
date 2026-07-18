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


def _new_run_path() -> Path:
    # One fresh file per run, named by local start time (this module imports once
    # per sidecar process, so the name is fixed for the life of the run).
    return LOG_DIR / f"cdrama-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"


# CDT_LOG_PATH pins a single explicit file (used by tests); otherwise each run
# gets its own timestamped file.
_explicit = os.getenv("CDT_LOG_PATH", "").strip()
LOG_PATH = Path(_explicit) if _explicit else _new_run_path()

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
