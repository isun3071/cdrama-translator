"""Audit the translation log — offline, over a run's jsonl (never in the hot path).

    python audit.py                     # stats over the MOST RECENT run
    python audit.py --all               # combine every run in the log dir
    python audit.py path/to.jsonl       # a specific run
    python audit.py --pipeline 20       # last 20 lines' full per-stage pipeline
    python audit.py --metrics           # the log-derived accuracy STACK (no LLM)
    python audit.py --judge 40 --lang en --label "情满四合院"

Why a stack, not a score: for a live-OCR pipeline "accuracy" is not one number.
A translation is only as good as the source OCR read (garbled source -> a faithful
but wrong line), the sliding window makes term scatter structural, and a live
system trades quality against latency. So we measure stages the log already
separates:
  --metrics (cheap, no LLM): OCR fidelity (confidence/consensus), terminology
     consistency (line stability + glossary adherence), latency & display health.
  --judge   (LLM, offline):  translation adequacy/fluency given the source, as
     GEMBA-MQM — error categories x severity, aggregated over independent votes.

The judge runs SEPARATELY on the log, not live: it is several LLM calls per line
and would blow the bounded-lag budget in the hot path. GROQ_JUDGE_MODEL picks the
grader (default = the translator model = a self-eval first pass, biased high — set
a different/stronger model for a real audit); GROQ_JUDGE_VOTES sets the vote count.
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

from audit_log import LOG_DIR
from judge_llm import complete, judge_session


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
        if r.get("context_note"):
            print(f"     note:   {r['context_note'][:90]}")
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


# --- GEMBA-MQM judge (reference-free; the only kind we can run — no gold) ------ #
# MQM error typology, trimmed to the categories a subtitle line actually shows and
# mapped to our known failure modes (addition = the name-tag/context leak; term =
# the glossary target; punctuation = the capitalization issue). Standard MQM
# severity weights: minor 1, major 5, critical 10.
_MQM_WEIGHT = {"minor": 1, "major": 5, "critical": 10}
_MQM_CATS = ("accuracy/mistranslation", "accuracy/omission", "accuracy/addition",
             "terminology", "fluency/grammar", "fluency/punctuation", "style/register")
_MQM_SYS = (
    "You are a strict MQM error annotator for Chinese->English TV-drama subtitle "
    "translations. Annotate errors in the ENGLISH only. The Chinese is OCR'd from "
    "burned-in subtitles and may contain recognition errors: judge the English "
    "against the plain meaning of the Chinese, never flag the Chinese itself, and "
    "do NOT penalize a faithful rendering of a garbled source. Subtitles are terse "
    "— a correct short line is not an omission. Categories: " + "|".join(_MQM_CATS) +
    ". Severities: minor|major|critical (reserve critical for meaning-breaking "
    'errors). Return ONLY JSON {"errors":[{"category","severity","span","note"}]}; '
    "an empty list means no error. Most lines have zero or one error."
)


def _mqm_vote(sess, url: str, model: str, ctx: str, src: str, trn: str,
              provider: str = "groq", temperature: float = 0.0) -> tuple[dict, dict]:
    """One MQM annotation pass. Returns ({category: worst_severity}, {category: note});
    raises on transport/parse failure. Routed through the configured grader provider."""
    user = (f"Context (reference, do not grade):\n{ctx or '(none)'}\n\n"
            f"Chinese: {src}\nEnglish: {trn}")
    content = complete(
        sess, url, model,
        [{"role": "system", "content": _MQM_SYS}, {"role": "user", "content": user}],
        provider=provider, response_format={"type": "json_object"}, max_tokens=400,
        temperature=temperature, timeout=30,
    )
    data = json.loads(content)
    catsev, notes = {}, {}
    for e in (data.get("errors") or []):
        c = e.get("category", "")
        c = c if c in _MQM_CATS else "other"
        s = e.get("severity", "minor")
        s = s if s in _MQM_WEIGHT else "minor"
        if _MQM_WEIGHT[s] > _MQM_WEIGHT.get(catsev.get(c), 0):
            catsev[c] = s  # keep the worst severity per category within a vote
        notes.setdefault(c, (e.get("note") or "").strip())
    return catsev, notes


def judge(rows: list[dict], n: int) -> None:
    sess, url, model, provider, has_key = judge_session()
    if not has_key:
        need = "OPENROUTER_API_KEY" if provider == "openrouter" else "GROQ_API_KEY"
        print(f"judge needs {need} for JUDGE_PROVIDER={provider} (repo-root .env).", file=sys.stderr)
        return
    translator_model = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
    votes = max(1, int(os.getenv("GROQ_JUDGE_VOTES", "3")))  # GEMBA-MQM v2: aggregate independent judgments
    maj = votes // 2 + 1
    temp = 0.4 if votes > 1 else 0.0   # votes must VARY to aggregate — temp 0 makes them identical

    pool = [r for r in rows if r.get("status") == "ok" and r.get("translation") and r.get("source_text")]
    if not pool:
        print("no ok translations to judge.", file=sys.stderr)
        return
    # Even coverage across the log without Math.random-style bias: stride sample.
    step = max(1, len(pool) // n)
    sample = pool[::step][:n]
    self_eval = provider == "groq" and model == translator_model
    print(f"\nGEMBA-MQM: {len(sample)} of {len(pool)} ok lines x {votes} vote(s), grader={model} via {provider}")
    print(f"  offline pass over the log (~{len(sample)*votes} calls)"
          + (f", votes @ temp {temp}" if votes > 1 else "")
          + ("  — SELF-EVAL: grader == translator, biased high; set JUDGE_MODEL / JUDGE_PROVIDER for a real audit" if self_eval else "")
          + "\n")

    scored = []
    for r in sample:
        ctx = "\n".join(r.get("context_lines") or [])
        src, trn = r["source_text"], r["translation"]
        catcount, worst, notes, penalties = collections.Counter(), {}, {}, []
        for _ in range(votes):
            try:
                catsev, vnotes = _mqm_vote(sess, url, model, ctx, src, trn, provider, temp)
            except Exception:
                continue  # a dead vote just doesn't count toward the majority
            penalties.append(sum(_MQM_WEIGHT[s] for s in catsev.values()))
            for c, s in catsev.items():
                catcount[c] += 1
                if _MQM_WEIGHT[s] > _MQM_WEIGHT.get(worst.get(c), 0):
                    worst[c] = s
            for c, note in vnotes.items():
                if note:
                    notes.setdefault(c, note)
        if not penalties:
            continue  # every vote errored — drop from the sample rather than fake a score
        confirmed = {c: worst[c] for c, cnt in catcount.items() if cnt >= maj}  # majority-voted only
        scored.append({"r": r, "pen": statistics.median(penalties), "confirmed": confirmed, "notes": notes})

    if not scored:
        print("all judge calls failed (network/model/key).", file=sys.stderr)
        return
    clean = sum(1 for x in scored if not x["confirmed"])
    mean_pen = statistics.mean(x["pen"] for x in scored)
    print(f"clean (no majority-confirmed error): {clean}/{len(scored)} ({100*clean//len(scored)}%)"
          f"   mean MQM penalty/line: {mean_pen:.2f}   (0 = perfect; minor 1 / major 5 / critical 10)\n")

    cat_inc, sev_inc = collections.Counter(), collections.Counter()
    for x in scored:
        for c, s in x["confirmed"].items():
            cat_inc[c] += 1
            sev_inc[s] += 1
    if cat_inc:
        print("confirmed error categories (share of judged lines):")
        for c, cnt in cat_inc.most_common():
            print(f"    {cnt:4d}  {100*cnt//len(scored):3d}%  {c}")
        print(f"  by severity: {dict(sev_inc)}\n")

    flagged = sorted((x for x in scored if x["confirmed"]), key=lambda x: -x["pen"])
    if flagged:
        print(f"worst lines (highest penalty first, top {min(15, len(flagged))}):")
        for x in flagged[:15]:
            r = x["r"]
            vt = r.get("video_time")
            loc = f"t={vt:.1f}s  " if isinstance(vt, (int, float)) else ""
            errs = ", ".join(f"{c.split('/')[-1]}:{s}" for c, s in x["confirmed"].items())
            note = "; ".join(n for n in x["notes"].values() if n)[:80]
            print(f"  [{x['pen']:.0f}] {loc}{r['source_text']}  ->  {r['translation']}")
            print(f"        {errs}" + (f"   «{note}»" if note else ""))
    else:
        print("no majority-confirmed errors in the sample.")


def metrics(rows: list[dict]) -> None:
    """The log-derived accuracy STACK — cheap, no LLM. For a live-OCR pipeline
    'accuracy' is not one number: OCR fidelity, terminology consistency, and
    latency each fail differently and the log already separates them."""
    tr, disp = split_stages(rows)
    ok = [r for r in tr if r.get("status") == "ok" and r.get("translation")]
    label = next((r.get("label") for r in tr if r.get("label")), "") or ""
    print("\n=== accuracy stack (log-derived, no LLM) ===")
    print(f"    {len(tr)} translate records · {len(ok)} shown ok · {len(disp)} display records"
          + (f"   label: {label[:48]}" if label else ""))

    # [1] OCR fidelity — a translation is only as good as the source it read; a
    # garbled source that is faithfully translated is a wrong line no MT metric
    # can catch, so we measure the OCR stage on its own.
    print("\n  [1] OCR fidelity  (source-side; garbled source -> faithful-but-wrong line)")
    confs = sorted(r["confidence"] for r in tr if r.get("confidence"))
    if confs:
        low = sum(c < 0.6 for c in confs)
        print(f"      confidence: p10 {_pct(confs,.1):.2f}  median {statistics.median(confs):.2f}"
              f"  mean {sum(confs)/len(confs):.2f}   <0.60: {low} ({100*low//len(confs)}%)")
    st = collections.Counter(r.get("status") for r in tr)
    if tr:
        print(f"      status mix: {dict(st)}   "
              f"no_text {100*st.get('no_text',0)//len(tr)}%  low_confidence {100*st.get('low_confidence',0)//len(tr)}%")
    multi = [r for r in tr if len([x for x in (r.get("reads") or []) if x.get("text")]) >= 2]
    disagree = [r for r in multi if len({x["text"] for x in r["reads"] if x.get("text")}) > 1]
    if multi:
        print(f"      frames disagree (vote had to arbitrate): {len(disagree)}/{len(multi)} "
              f"({100*len(disagree)//len(multi)}%)  — OCR-uncertainty proxy")

    # [2] terminology consistency — the sliding context window makes scatter
    # structural, so consistency is a first-class metric, not an afterthought.
    print("\n  [2] terminology consistency  (the sliding window makes scatter structural)")
    by_src = collections.defaultdict(list)
    for r in ok:
        by_src[r["source_text"]].append(r["translation"])
    repeated = {s: v for s, v in by_src.items() if len(v) >= 2}
    unstable = {s: list(dict.fromkeys(v)) for s, v in repeated.items() if len(set(v)) >= 2}
    if repeated:
        print(f"      identical-line stability: {len(repeated)-len(unstable)}/{len(repeated)} "
              f"recurring lines rendered the same every time"
              + (f"; {len(unstable)} drifted:" if unstable else ""))
        for s, variants in list(unstable.items())[:4]:
            print(f"        {s}")
            for v in variants[:3]:
                print(f"            -> {v}")
    else:
        print("      identical-line stability: no source line recurred (nothing to check)")
    try:
        from glossary import GLOSSARY
        terms = GLOSSARY.all_terms(label)
    except Exception:
        terms = {}
    adh = []
    for zh, en in terms.items():
        hits = [r for r in ok if zh in r["source_text"]]
        if len(hits) >= 2:
            kept = sum(en.lower() in (r["translation"] or "").lower() for r in hits)
            adh.append((zh, en, kept, len(hits)))
    if adh:
        tot = sum(t for *_, t in adh)
        kept = sum(k for *_, k, _ in adh)
        print(f"      glossary adherence: {kept}/{tot} occurrences of recurring pinned terms used the "
              f"pinned English ({100*kept//max(1,tot)}%)  [substring match — undercounts short terms]")
        for zh, en, k, t in sorted(adh, key=lambda x: x[2] / x[3]):
            if k < t:
                print(f"        {zh} = {en}: {k}/{t} lines")
    elif terms:
        print("      glossary adherence: no pinned term recurred >=2x in this run")

    # [3] latency & live health — quality alone is the wrong frame for a live
    # system; it trades against latency, and a great line shown too late is waste.
    print("\n  [3] latency & live health  (quality x latency is the real axis)")
    lats = sorted(r["latency_ms"] for r in tr if r.get("latency_ms"))
    if lats:
        over = sum(l > 600 for l in lats)
        print(f"      latency ms: p50 {_pct(lats,.5)}  p90 {_pct(lats,.9)}  p95 {_pct(lats,.95)}  max {lats[-1]}"
              f"   >600ms: {over} ({100*over//len(lats)}%)")
    if disp:
        oc = collections.Counter(d.get("outcome") for d in disp)
        dropped = oc.get("dropped", 0)
        print(f"      display outcomes: {dict(oc)}")
        print(f"      dropped (translated but never shown): {dropped} ({100*dropped//len(disp)}%)"
              f"  — wasted work / arrived too late")
        vis = sorted(d["visible_ms"] for d in disp if d.get("visible_ms"))
        if vis:
            brief = sum(v < 800 for v in vis)
            print(f"      visible ms: p10 {_pct(vis,.1)}  median {int(statistics.median(vis))}"
                  f"   <800ms: {brief} ({100*brief//len(vis)}%)  — too brief to read")
    else:
        print("      no display records (extension /log not reporting, or logging off)")


def _latest_log() -> Path | None:
    files = sorted(LOG_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="a specific log file (default: the most recent run)")
    ap.add_argument("--all", action="store_true", help="combine every run in the log dir")
    ap.add_argument("--metrics", action="store_true", help="the log-derived accuracy stack (OCR/consistency/latency, no LLM)")
    ap.add_argument("--judge", type=int, metavar="N", help="GEMBA-MQM judge N sampled ok lines (LLM, offline)")
    ap.add_argument("--pipeline", type=int, metavar="N", help="print the last N lines' full per-stage pipeline")
    ap.add_argument("--lang", help="filter to a target_lang")
    ap.add_argument("--label", help="filter to a label substring")
    args = ap.parse_args()

    if args.all:
        files = sorted(LOG_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not files:
            print(f"no logs in {LOG_DIR} — watch something with the sidecar running first.", file=sys.stderr)
            return 1
        rows = [r for p in files for r in load(p)]
        print(f"loaded {len(files)} run(s) from {LOG_DIR}")
    else:
        path = Path(args.path) if args.path else _latest_log()
        if not path or not path.exists():
            print(f"no log found in {LOG_DIR} — watch something with the sidecar running first.", file=sys.stderr)
            return 1
        print(f"log: {path.name}")
        rows = load(path)

    if args.lang:
        rows = [r for r in rows if r.get("target_lang") == args.lang]
    if args.label:
        rows = [r for r in rows if args.label in (r.get("label") or "")]

    stats(rows)
    if args.metrics:
        metrics(rows)
    if args.pipeline:
        pipeline(rows, args.pipeline)
    if args.judge:
        tr, _ = split_stages(rows)
        judge(tr, args.judge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
