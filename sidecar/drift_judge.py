"""Drift-tagged judge — CROSS-LINE consistency errors only. The gate for Ideas 2/3.

Runs offline over a run's jsonl (never live). For each target line it shows the
grader a window i-2..i+1 (source hanzi + the text actually shown) with the target
marked, and asks ONLY about errors that arise from the relationship BETWEEN lines
— not standalone mistranslations. Categories:

  cross-speaker-leak, stale-referent, register-drift, tense-aspect-flip,
  pronoun-gender-inconsistency

Leak/referent errors must carry source_scope (immediate-neighbor | scene-level).
That field is the Idea-3 decision signal: per the pre-registered rule, the
scene-summarizer earns a mandate ONLY when meaning-changing leaks are predominantly
scene-level. immediate-neighbor leaks implicate the continuation arbiter instead.

Two samples, different jobs (do not conflate):
  default (random)  -> the population GATE rate — the number the decision rule keys
                       on. Judge output is judge-flagged, NOT ground truth: verify
                       the confirmed set by hand before treating the rate as real.
  --stratified      -> oversamples risky windows (continuation / pronoun context) to
                       characterize SHAPE & severity. It over-estimates frequency —
                       never use it for the gate.

Grader defaults to a NON-Qwen model (the translator is Qwen; avoid self-eval).
Votes are aggregated (majority-confirmed) like GEMBA-MQM.

    python drift_judge.py [log] [--sample N] [--stratified] [--votes V]
    env: GROQ_DRIFT_MODEL (default llama-3.3-70b-versatile), GROQ_DRIFT_VOTES (=3)

Confirmed instances are printed with their full window AND written to
<log>.drift-verify.jsonl for the human verification pass.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from audit import load, split_stages, _latest_log
from audit_log import LOG_DIR

_SEV = {"minor": 1, "major": 5, "critical": 10}
_CATS = ("cross-speaker-leak", "stale-referent", "register-drift",
         "tense-aspect-flip", "pronoun-gender-inconsistency")
_SCOPE_CATS = {"cross-speaker-leak", "stale-referent"}  # these must name a source_scope
_SCOPES = ("immediate-neighbor", "scene-level")
_PRON = re.compile(r"[他她它你我咱您]|们")

_SYS = (
    "You audit a STREAMING Chinese->English subtitle translator for CROSS-LINE "
    "consistency errors only. You see a window of consecutive lines (earliest "
    "first); each has the Chinese (zh, OCR'd from burned-in subtitles — ignore OCR "
    "garble) and the English shown (en). Judge ONLY the line marked >> TARGET, using "
    "the others as context. The translator saw only lines up to the target (no "
    "lookahead). Report ONLY errors arising from the RELATIONSHIP BETWEEN lines, "
    "never a standalone mistranslation you'd flag from the target alone.\n"
    "Categories:\n"
    "- cross-speaker-leak: content from a DIFFERENT speaker's line appears in the "
    "target's English (a name, object, clause that isn't in the target's own zh). "
    "NOT: correctly resolving a pronoun from context.\n"
    "- stale-referent: a pronoun/noun resolves to an earlier entity no longer the "
    "subject. NOT: correct carry-over of an ongoing referent.\n"
    "- register-drift: the target's tone clashes with the scene's established "
    "register, UNMOTIVATED by its own zh. NOT: a shift actually present in the zh.\n"
    "- tense-aspect-flip: English tense/aspect flips jarringly across consecutive "
    "lines for the same action (target-side; Chinese has no tense).\n"
    "- pronoun-gender-inconsistency: the same referent gets a different gender "
    "pronoun across the window (他 'he' then 'she'). NOT: different referents.\n"
    "For cross-speaker-leak and stale-referent you MUST set source_scope: "
    "'immediate-neighbor' if the bad content came from an adjacent line, "
    "'scene-level' if it's a stable referent/topic the translator should have "
    "tracked across the scene. Severities: minor|major|critical (major/critical only "
    "when meaning changes). Return ONLY JSON "
    '{"errors":[{"category","severity","span","source_scope","note"}]}; empty list '
    "means the target is drift-clean. Most targets are clean — do not invent drift."
)


def _shown_map(disp: list[dict]) -> dict:
    """frame_id -> the text actually shown (final_text), when the log has it."""
    m = {}
    for d in disp:
        fid, ft = d.get("frame_id"), d.get("final_text")
        if fid is not None and ft:
            m[fid] = ft
    return m


def _episodes(tr: list[dict]) -> list[list[dict]]:
    """Group ok lines into episodes in file (chronological) order — robust for old
    logs without episode_id (video_time can reset on seek; append order can't)."""
    groups: dict = {}
    order: list = []
    for r in tr:
        k = r.get("episode_id") or r.get("label") or "unknown"
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    return [groups[k] for k in order]


def _window(ep: list[dict], i: int, shown: dict) -> list[dict]:
    out = []
    for j in range(max(0, i - 2), min(len(ep), i + 2)):
        r = ep[j]
        en = shown.get(r.get("frame_id")) or r.get("translation") or ""
        out.append({"src": r.get("source_text", ""), "en": en, "is_target": j == i})
    return out


def _risky(row: dict) -> bool:
    if row.get("continuation"):
        return True
    return bool(_PRON.search(" ".join(row.get("context_lines") or [])))


def _drift_vote(sess, model: str, window: list[dict]) -> dict:
    """One annotation pass -> {category: (worst_severity, scope)}; raises on failure."""
    lines = []
    for w in window:
        tag = ">> TARGET" if w["is_target"] else "line     "
        lines.append(f"{tag} zh: {w['src']}   en: {w['en']}")
    user = "WINDOW (earliest first):\n" + "\n".join(lines) + "\n\nAnnotate only the >> TARGET line."
    body = {"model": model, "temperature": 0.0,
            "messages": [{"role": "system", "content": _SYS}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}, "max_tokens": 500}
    if any(k in model.lower() for k in ("qwen", "qwq")):
        body["reasoning_effort"] = "none"
    resp = sess.post("https://api.groq.com/openai/v1/chat/completions", json=body, timeout=40)
    resp.raise_for_status()
    data = json.loads(resp.json()["choices"][0]["message"]["content"])
    out: dict = {}
    for e in (data.get("errors") or []):
        c = e.get("category")
        if c not in _CATS:
            continue
        s = e.get("severity")
        s = s if s in _SEV else "minor"
        scope = e.get("source_scope")
        scope = scope if scope in _SCOPES else None
        prev = out.get(c)
        if prev is None or _SEV[s] > _SEV[prev[0]]:
            out[c] = (s, scope or (prev[1] if prev else None))
        elif scope and not prev[1]:
            out[c] = (prev[0], scope)
    return out


def run(rows: list[dict], n: int, stratified: bool, out_path: Path) -> None:
    import requests

    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        print("drift judge needs GROQ_API_KEY (repo-root .env).", file=sys.stderr)
        return
    model = os.getenv("GROQ_DRIFT_MODEL", os.getenv("GROQ_JUDGE_MODEL", "llama-3.3-70b-versatile"))
    if "qwen" in model.lower():
        print("WARNING: grader is a Qwen model — same family as the translator (self-eval bias).", file=sys.stderr)
    votes = max(1, int(os.getenv("GROQ_DRIFT_VOTES", "3")))
    maj = votes // 2 + 1
    sess = requests.Session()
    sess.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})

    tr, disp = split_stages(rows)
    ok = [r for r in tr if r.get("status") == "ok" and r.get("source_text")]
    shown = _shown_map(disp)

    # Eligible targets: any line with >=1 prior line in its episode (drift needs a
    # predecessor). Collect (episode, index) then sample.
    targets = []
    for ep in _episodes(ok):
        for i in range(1, len(ep)):
            if stratified and not _risky(ep[i]):
                continue
            targets.append((ep, i))
    if not targets:
        print("no eligible windows (need consecutive same-episode lines).", file=sys.stderr)
        return
    step = max(1, len(targets) // n)
    sample = targets[::step][:n]

    mode = "STRATIFIED (risky windows; over-estimates frequency — SHAPE only)" if stratified \
        else "RANDOM (population GATE rate)"
    print(f"\ndrift judge — {mode}")
    print(f"  {len(sample)} of {len(targets)} eligible windows x {votes} vote(s), grader={model}")
    print(f"  ~{len(sample)*votes} calls, offline. Judge-flagged, NOT verified — confirm by hand.\n")

    confirmed_rows = []
    for ep, i in sample:
        window = _window(ep, i, shown)
        catcount = collections.Counter()
        worst: dict = {}
        scopes: dict = collections.defaultdict(collections.Counter)
        notes: dict = {}
        ran = 0
        for _ in range(votes):
            try:
                v = _drift_vote(sess, model, window)
            except Exception:
                continue
            ran += 1
            for c, (s, scope) in v.items():
                catcount[c] += 1
                if c not in worst or _SEV[s] > _SEV[worst[c]]:
                    worst[c] = s
                if scope:
                    scopes[c][scope] += 1
        if not ran:
            continue
        conf = {}
        for c, cnt in catcount.items():
            if cnt >= maj:
                scope = scopes[c].most_common(1)[0][0] if scopes[c] else None
                conf[c] = {"severity": worst[c], "source_scope": scope}
        if conf:
            tgt = ep[i]
            confirmed_rows.append({
                "episode_id": tgt.get("episode_id"), "frame_id": tgt.get("frame_id"),
                "video_time": tgt.get("video_time"), "line_seq": tgt.get("line_seq"),
                "target_zh": tgt.get("source_text"), "target_en": window[[w["is_target"] for w in window].index(True)]["en"],
                "window": [{"zh": w["src"], "en": w["en"], "target": w["is_target"]} for w in window],
                "confirmed": conf, "votes": votes,
                "verified": None,  # <- the human fills this in
            })

    judged = len(sample)
    flagged = len(confirmed_rows)
    print(f"windows with >=1 majority-confirmed drift error: {flagged}/{judged} "
          f"({100*flagged//max(1,judged)}%)   [{'GATE — verify first' if not stratified else 'SHAPE — not the gate'}]\n")

    cat_inc = collections.Counter()
    sev_inc = collections.Counter()
    scope_inc = collections.Counter()
    for r in confirmed_rows:
        for c, d in r["confirmed"].items():
            cat_inc[c] += 1
            sev_inc[d["severity"]] += 1
            if c in _SCOPE_CATS and d["source_scope"]:
                scope_inc[d["source_scope"]] += 1
    if cat_inc:
        print("confirmed categories (share of judged windows):")
        for c, cnt in cat_inc.most_common():
            print(f"    {cnt:4d}  {100*cnt//max(1,judged):3d}%  {c}")
        print(f"  severity: {dict(sev_inc)}")
        print(f"  leak/referent source_scope: {dict(scope_inc)}   "
              f"<- scene-level is the Idea-3 signal; immediate-neighbor => arbiter\n")

    # Dump confirmed instances for the human verification pass.
    if confirmed_rows:
        with out_path.open("w", encoding="utf-8") as f:
            for r in confirmed_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"confirmed instances -> {out_path}  (set \"verified\": true/false per line)\n")
        for r in confirmed_rows[:12]:
            errs = ", ".join(f"{c}:{d['severity']}" + (f"/{d['source_scope']}" if d['source_scope'] else "")
                             for c, d in r["confirmed"].items())
            vt = r.get("video_time")
            loc = f"t={vt:.1f}s " if isinstance(vt, (int, float)) else ""
            print(f"  {loc}[{errs}]")
            for w in r["window"]:
                mark = ">>" if w["target"] else "  "
                print(f"    {mark} {w['zh']}  ->  {w['en']}")
            print()
    else:
        print("no confirmed drift in the sample.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="log file (default: most recent run)")
    ap.add_argument("--sample", type=int, default=60, help="target windows to judge (default 60)")
    ap.add_argument("--stratified", action="store_true", help="oversample risky windows (SHAPE, not the gate)")
    ap.add_argument("--votes", type=int, help="votes per window (else GROQ_DRIFT_VOTES or 3)")
    args = ap.parse_args()
    if args.votes:
        os.environ["GROQ_DRIFT_VOTES"] = str(args.votes)

    path = Path(args.path) if args.path else _latest_log()
    if not path or not path.exists():
        print(f"no log found in {LOG_DIR}.", file=sys.stderr)
        return 1
    print(f"log: {path.name}")
    rows = load(path)
    suffix = "stratified" if args.stratified else "random"
    out_path = path.with_suffix(f".drift-{suffix}.verify.jsonl")
    run(rows, args.sample, args.stratified, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
