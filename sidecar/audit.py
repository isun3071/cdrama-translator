"""Audit the translation log: stats always, LLM-judged accuracy on request.

    python audit.py                     # stats over the default log
    python audit.py path/to.jsonl       # stats over a specific log
    python audit.py --judge 40          # + LLM-judge a random-ish sample of 40
    python audit.py --judge 40 --lang en --label "情满四合院"

The judge scores each shown (source -> translation) pair 1-5 for accuracy and
flags issues. Caveat: by default it uses the SAME Groq model that produced the
translations, so it's a self-eval first pass (good for surfacing suspects, not a
ground truth) — set GROQ_JUDGE_MODEL to a stronger/different model for a real
audit, or export the flagged rows for human review.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
from pathlib import Path

try:  # so --judge can reach GROQ_API_KEY; stats works without it
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from audit_log import LOG_PATH


def load(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _pct(sorted_vals, q):
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def split_stages(rows: list[dict]):
    """The log interleaves 'translate' records (sidecar: OCR->vote->translate) and
    'display' records (extension: what the overlay did). Old records without a
    stage are treated as translate."""
    tr = [r for r in rows if r.get("stage", "translate") == "translate"]
    disp = [r for r in rows if r.get("stage") == "display"]
    return tr, disp


def pipeline(rows: list[dict], n: int) -> None:
    """Join translate+display by frame_id and print the full per-line pipeline —
    raw reads -> voted -> context/↳cont -> translation -> display outcome — so a
    bad output can be traced to the stage it went wrong."""
    tr, disp = split_stages(rows)
    disp_by_id = {d["frame_id"]: d for d in disp if "frame_id" in d}
    shown = [r for r in tr if r.get("status") in ("ok", "low_confidence")][-n:]
    print(f"\n--- per-line pipeline (last {len(shown)}) ---")
    for r in shown:
        reads = " | ".join(f"{x.get('text','')}" for x in (r.get("reads") or []))
        cont = " ↳cont" if r.get("continuation") else ""
        d = disp_by_id.get(r.get("frame_id"))
        disp_s = f"{d['outcome']} {d.get('visible_ms',0)}ms" if d else "—"
        vt = r.get("video_time") or 0
        print(f"  t={vt:7.1f}s  [{r.get('status')}]{cont}")
        print(f"     reads:  {reads}")
        print(f"     voted:  {r.get('source_text','')}   (conf {r.get('confidence')})")
        if r.get("context_lines"):
            print(f"     ctx:    {' / '.join(r['context_lines'])}")
        print(f"     -> {r.get('translation') or '(none)'}   [display: {disp_s}]")


def stats(rows: list[dict]) -> None:
    rows, _disp = split_stages(rows)
    n = len(rows)
    print(f"lines logged: {n}")
    if not n:
        return
    by_status = collections.Counter(r.get("status") for r in rows)
    print("  by status:", dict(by_status))
    print("  by target_lang:", dict(collections.Counter(r.get("target_lang") for r in rows)))
    labels = collections.Counter((r.get("label") or "(none)") for r in rows)
    print("  by label (episodes):")
    for lbl, c in labels.most_common(12):
        print(f"     {c:5d}  {lbl[:70]}")

    confs = [r["confidence"] for r in rows if r.get("confidence")]
    if confs:
        cs = sorted(confs)
        print(f"  OCR confidence: median {statistics.median(cs):.2f}  mean {sum(cs)/len(cs):.2f}"
              f"  <0.6: {sum(c < 0.6 for c in cs)} ({100*sum(c<0.6 for c in cs)//len(cs)}%)")
    lats = sorted(r["latency_ms"] for r in rows if r.get("latency_ms"))
    if lats:
        print(f"  latency ms: p50 {_pct(lats,.5)}  p90 {_pct(lats,.9)}  p99 {_pct(lats,.99)}  max {lats[-1]}")
    print(f"  continuation candidates: {sum(1 for r in rows if r.get('continuation'))}"
          f"   duplicates: {sum(1 for r in rows if r.get('duplicate'))}"
          f"   ok w/ translation: {sum(1 for r in rows if r.get('status')=='ok' and r.get('translation'))}")
    if _disp:
        oc = collections.Counter(d.get("outcome") for d in _disp)
        brief = sum(oc.get(k, 0) for k in ("preempted", "dropped"))
        print(f"  display outcomes: {dict(oc)}  (shown-too-briefly = {brief})")


def judge(rows: list[dict], n: int) -> None:
    import requests

    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        print("judge needs GROQ_API_KEY (in the repo-root .env or the env).", file=sys.stderr)
        return
    model = os.getenv("GROQ_JUDGE_MODEL", os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"))
    sess = requests.Session()
    sess.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})

    pool = [r for r in rows if r.get("status") == "ok" and r.get("translation") and r.get("source_text")]
    # Even coverage across the log without Math.random-style bias: stride sample.
    step = max(1, len(pool) // n)
    sample = pool[::step][:n]
    print(f"\njudging {len(sample)} of {len(pool)} ok lines with {model} (self-eval caveat applies)\n")

    SYS = ("You grade Chinese->English subtitle translations. Reply ONLY as compact JSON "
           '{"score": <1-5>, "issue": "<=8 words or empty"}. 5 = accurate and natural, '
           "1 = wrong or nonsense. Judge whether the English conveys the Chinese meaning "
           "and reads naturally for a subtitle.")
    scored = []
    for r in sample:
        ctx = "\n".join(r.get("context_lines") or [])
        src = r["source_text"]
        prompt = (f"Context (prior lines, reference):\n{ctx or '(none)'}\n\n"
                  f"Chinese: {src}\nEnglish shown: {r['translation']}")
        try:
            resp = sess.post("https://api.groq.com/openai/v1/chat/completions", json={
                "model": model, "temperature": 0,
                "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}, "max_tokens": 60,
            }, timeout=20)
            resp.raise_for_status()
            verdict = json.loads(resp.json()["choices"][0]["message"]["content"])
            scored.append((int(verdict.get("score", 0)), verdict.get("issue", ""), r))
        except Exception as e:
            scored.append((0, f"judge error: {type(e).__name__}", r))

    dist = collections.Counter(s for s, _, _ in scored)
    good = [x for x in scored if x[0] >= 4]
    print(f"score distribution: {dict(sorted(dist.items()))}")
    print(f"mean score: {statistics.mean([s for s,_,_ in scored if s]):.2f}"
          f"   >=4: {len(good)}/{len(scored)} ({100*len(good)//max(1,len(scored))}%)\n")
    print("flagged (score <= 3), worst first:")
    for score, issue, r in sorted(scored, key=lambda x: x[0]):
        if score <= 3:
            print(f"  [{score}] {r['source_text']}  ->  {r['translation']}"
                  f"{'   («'+issue+'»)' if issue else ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=str(LOG_PATH))
    ap.add_argument("--judge", type=int, metavar="N", help="LLM-judge N sampled ok lines")
    ap.add_argument("--pipeline", type=int, metavar="N", help="print the last N lines' full per-stage pipeline")
    ap.add_argument("--lang", help="filter to a target_lang")
    ap.add_argument("--label", help="filter to a label substring")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"no log at {path} — watch something with the sidecar running first.", file=sys.stderr)
        return 1
    rows = load(path)
    if args.lang:
        rows = [r for r in rows if r.get("target_lang") == args.lang]
    if args.label:
        rows = [r for r in rows if args.label in (r.get("label") or "")]

    stats(rows)
    if args.pipeline:
        pipeline(rows, args.pipeline)
    if args.judge:
        tr, _ = split_stages(rows)
        judge(tr, args.judge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
