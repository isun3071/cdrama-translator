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


# Fixed at import = the run stamp. The model tag is filled in by set_run_tag() at
# startup; the episode slug by set_episode() whenever the video changes, so one run
# watching several videos self-separates into a file per video.
_STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
_run_tag = ""
_episode = ""
_episode_slug = ""
_log_path: Path | None = None


def set_run_tag(tag: str) -> None:
    """Tag this run's file with e.g. the model slug, so llama and qwen runs land
    in differently-named files (cdrama-<stamp>-<tag>.jsonl)."""
    global _run_tag, _log_path
    base = (tag or "").split("/")[-1]
    _run_tag = "".join(c if (c.isalnum() or c in "._-") else "-" for c in base)[:40]
    _log_path = None  # re-resolve on next append


def _slug(label: str) -> str:
    s = "".join(c if (c.isalnum() or c in "._-" or "一" <= c <= "鿿") else "-"
                for c in (label or "").strip())
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:24]


def set_episode(ep_id: str, label: str = "") -> None:
    """Rotate to a per-video file when the episode changes (new video -> new file),
    so a single run needn't be restarted per episode. Keyed on the stable episode_id;
    the readable slug comes from the label. No-op when CDT_LOG_PATH pins one file."""
    global _episode, _episode_slug, _log_path
    if not ENABLED or os.getenv("CDT_LOG_PATH", "").strip():
        return
    if ep_id and ep_id != _episode:
        _episode = ep_id
        _episode_slug = _slug(label) or ep_id
        _log_path = None  # re-resolve to the new video's file


def _resolve_path() -> Path:
    # CDT_LOG_PATH pins a single explicit file (used by tests); else per-run-per-video.
    explicit = os.getenv("CDT_LOG_PATH", "").strip()
    if explicit:
        return Path(explicit)
    suffix = f"-{_run_tag}" if _run_tag else ""
    ep = f"-{_episode_slug}" if _episode_slug else ""
    return LOG_DIR / f"cdrama-{_STAMP}{suffix}{ep}.jsonl"

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
