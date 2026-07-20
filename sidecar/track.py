"""Build a personal re-watch TRACK from a run's log — offline, high-quality.

Live translation is bounded-lag / mostly-right and sees only PAST context. This is
the other corner of the frontier: an offline pass where a strong "teacher" model
re-translates every line with a window of surrounding lines — past AND future — the
context the live model never had. The result is written as the tool's own
frame_id-keyed replay track (CLAUDE.md invariant 1): a LOCAL, personal artifact for
re-watching your own in-the-clear video, deliberately NOT a portable .srt.

Each cue carries: frame_id (the pixel-line id), t (video seconds — when it should
show), dur, the source hanzi, the corrected `text`, and the original `live` line for
comparison. The extension's replay mode plays these against the video's currentTime.

Reuses judge_llm's provider routing + cost tally, so the teacher can be any strong
Chinese-capable model (TEACHER_MODEL, else JUDGE_MODEL). A track is a PER-EPISODE
personal artifact. Its outputs can also seed the distillation teacher pass — but only
across the corpus gate (>=3 shows / >=2 genres), never one show, or the student
overfits that show (CLAUDE.md invariant 1).

    python track.py [log]                 # -> <log>.track.json (latest run by default)
    python track.py [log] --out mytrack.json --window 8
    env: TEACHER_MODEL (grader/teacher), JUDGE_PROVIDER, OPENROUTER_API_KEY / GROQ_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from audit import load, split_stages, _latest_log
from audit_log import LOG_DIR
from drift_judge import _episodes
from judge_llm import complete, cost_estimate, judge_session, reset_usage
from translate import _LANG, _system_prompt

_CUE_MIN, _CUE_MAX, _CUE_DEFAULT = 0.8, 8.0, 3.0   # seconds


def _teacher_sys(lang: str) -> str:
    """The live system prompt's house RULES (name-tag elision, interrogative-without-吗,
    no context leak, register, OCR-noise tolerance, output-only) — so the track matches
    the live style — re-framed for the offline pass, which additionally has FUTURE
    context. Strip the 'live subtitles' framing sentence and prepend the teacher one."""
    base = _system_prompt(lang)
    live = f"You are translating live subtitles for a Chinese TV drama into {lang}. "
    rules = base[len(live):] if base.startswith(live) else base
    return (
        f"You are producing the definitive {lang} subtitle for a Chinese TV drama, OFFLINE, with "
        f"surrounding lines as context — including lines AFTER the one you translate, which a live "
        f"translator never sees. Use them to resolve names, references, register and split "
        f"sentences. " + rules
    )


def _finalize(cues: list[dict]) -> list[dict]:
    """Sort by (episode, time), drop re-visited lines (a seek-back during capture logs
    the same line twice at ~the same t), then set each cue's duration from the NEXT
    cue in time order — so durations are right even if capture order wasn't monotonic."""
    cues = sorted(cues, key=lambda c: (c["episode_id"] or "", c["t"]))
    out: list[dict] = []
    dropped = 0
    for c in cues:
        p = out[-1] if out else None
        if (p and p["episode_id"] == c["episode_id"] and p["source"] == c["source"]
                and abs(c["t"] - p["t"]) < 2.0):
            dropped += 1
            continue   # same line, ~same moment -> a re-visit, keep just one
        out.append(c)
    for i, c in enumerate(out):
        nxt = out[i + 1] if i + 1 < len(out) and out[i + 1]["episode_id"] == c["episode_id"] else None
        c["dur"] = round(_CUE_DEFAULT if not nxt else max(_CUE_MIN, min(_CUE_MAX, nxt["t"] - c["t"])), 3)
    if dropped:
        print(f"  deduped {dropped} re-visited line(s) (seek/rewind during capture)", file=sys.stderr)
    return out


def _teacher(sess, url, model, provider, lang, before, target, after) -> str:
    ctx = []
    if before:
        ctx.append("Earlier lines:\n" + "\n".join(before))
    if after:
        ctx.append("Later lines:\n" + "\n".join(after))
    user = ("\n\n".join(ctx) + "\n\n" if ctx else "") + f">> Translate this line:\n{target}"
    out = complete(
        sess, url, model,
        [{"role": "system", "content": _teacher_sys(lang)},
         {"role": "user", "content": user}],
        provider=provider, max_tokens=200, temperature=0.0, timeout=40,
    )
    if "</think>" in out:
        out = out.split("</think>")[-1]
    return out.strip().strip('"').strip()


def build(rows: list[dict], out_path: Path, target_lang: str, window: int) -> None:
    sess, url, model, provider, has_key = judge_session(os.getenv("TEACHER_MODEL") or None)
    if not has_key:
        need = "OPENROUTER_API_KEY" if provider == "openrouter" else "GROQ_API_KEY"
        print(f"track needs {need} for JUDGE_PROVIDER={provider} (repo-root .env).", file=sys.stderr)
        return
    lang = _LANG.get(target_lang, target_lang)
    reset_usage()

    tr, _disp = split_stages(rows)
    ok = [r for r in tr if r.get("status") == "ok" and r.get("source_text")
          and r.get("target_lang", "en") == target_lang]
    episodes = _episodes(ok)
    total = sum(len(e) for e in episodes)
    if not total:
        print(f"no ok {target_lang} lines to rewrite.", file=sys.stderr)
        return
    print(f"\ntrack: rewriting {total} line(s) across {len(episodes)} episode(s) with "
          f"teacher={model} via {provider}  (±{window}-line context, incl. future)\n")

    cues, done = [], 0
    for ep in episodes:
        srcs = [r.get("source_text", "") for r in ep]
        for i, r in enumerate(ep):
            before = srcs[max(0, i - window):i]
            after = srcs[i + 1:i + 1 + window]
            try:
                text = _teacher(sess, url, model, provider, lang, before, r["source_text"], after)
            except Exception as e:
                text = r.get("translation", "") or ""   # fall back to the live line on failure
                print(f"  (line {r.get('frame_id')}: teacher failed [{type(e).__name__}], kept live)", file=sys.stderr)
            cues.append({
                "frame_id": r.get("frame_id"), "seq": r.get("line_seq"), "group": r.get("sentence_group_id"),
                "episode_id": r.get("episode_id"), "t": round(float(r.get("video_time") or 0.0), 3),
                "source": r["source_text"], "text": text, "live": r.get("translation", ""),
            })
            done += 1
            if done % 25 == 0 or done == total:
                print(f"  {done}/{total} …", file=sys.stderr)

    cues = _finalize(cues)   # sort by time, dedup re-visits, set durations from time-neighbors
    label = next((r.get("label") for r in ok if r.get("label")), "") or ""
    track = {
        "type": "cdt-replay-track", "version": 1,
        "note": "PERSONAL re-watch track — local only, not a portable subtitle file (CLAUDE.md invariant 1)",
        "target_lang": target_lang, "label": label, "teacher": model,
        "episode_ids": sorted({c["episode_id"] for c in cues if c["episode_id"]}),
        "cues": cues,
    }
    out_path.write_text(json.dumps(track, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\ntrack -> {out_path}  ({len(cues)} cues)")
    ce = cost_estimate()
    if ce:
        print(ce)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="log file (default: most recent run)")
    ap.add_argument("--out", help="output track file (default: <log>.track.json)")
    ap.add_argument("--lang", default="en", help="target_lang to build the track for")
    ap.add_argument("--window", type=int, default=8, help="context lines each side (incl. future)")
    args = ap.parse_args()

    path = Path(args.path) if args.path else _latest_log()
    if not path or not path.exists():
        print(f"no log found in {LOG_DIR}.", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else path.with_suffix(".track.json")
    print(f"log: {path.name}")
    build(load(path), out, args.lang, args.window)
    return 0


if __name__ == "__main__":
    sys.exit(main())
