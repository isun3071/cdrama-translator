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

CORPUS GATE: a number from ONE episode fits that episode, not the phenomenon. The
tool reports PER SHOW before aggregating and DEFERS if per-show rates diverge >2x;
below 3 shows it banners the run as WIRING VALIDATION ONLY, not evidence. Feed the
broader corpus with multiple logs or --all, and reserve a validation episode with
--holdout so you can tell whether a fix generalizes.

    python drift_judge.py [log ...] [--all] [--holdout SUBSTR] [--sample N] [--stratified] [--votes V]
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
from judge_llm import complete, cost_estimate, judge_session, reset_usage, voters

_SEV = {"minor": 1, "major": 5, "critical": 10}
_CATS = ("cross-speaker-leak", "stale-referent", "register-drift",
         "tense-aspect-flip", "pronoun-gender-inconsistency")
_SCOPE_CATS = {"cross-speaker-leak", "stale-referent"}  # these must name a source_scope
_SCOPES = ("immediate-neighbor", "scene-level")
_PRON = re.compile(r"[他她它你我咱您]|们")

# Corpus gate — guard against a single episode (or stray-tab noise) passing as a
# corpus. A show counts only if it clears _GATE_MIN_LINES; a per-show rate enters
# the divergence check only with _MIN_SHOW_SAMPLE judged items behind it.
_GATE_MIN_SHOWS = 3
_GATE_MIN_LINES = 30
_MIN_SHOW_SAMPLE = 8

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


# --- corpus scoping (measure across shows, never one episode) ---------------- #
def _show_key(label: str) -> str:
    """Best-effort SHOW (series) identity from the page label, so episodes of one
    show group together. Prefers the 《...》 title; else the text before a 第N集
    marker or a separator. Heuristic — a real multi-show corpus can be spot-checked
    against this in the per-show table."""
    if not label:
        return "unknown"
    m = re.search(r"《([^》]+)》", label)
    if m:
        return m.group(1).strip()
    s = re.split(r"第\s*\d+\s*集|[|｜\-]", label)[0].strip()
    return s[:40] or "unknown"


def _corpus(paths: list[str], use_all: bool, holdout: str | None) -> tuple[list[dict], list[Path]]:
    if use_all:
        files = sorted(LOG_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    elif paths:
        files = [Path(p) for p in paths]
    else:
        lp = _latest_log()
        files = [lp] if lp else []
    files = [f for f in files if f and f.exists()]
    rows: list[dict] = []
    for f in files:
        rows += load(f)
    if holdout:  # reserve a validation episode: never used for the pass
        rows = [r for r in rows if holdout not in (r.get("label") or "")]
    return rows, files


def _corpus_banner(rows: list[dict], files: list[Path], holdout: str | None) -> None:
    tr = [r for r in rows if r.get("stage", "translate") == "translate" and r.get("status") == "ok"]
    shows = collections.Counter(_show_key(r.get("label", "")) for r in tr)
    substantial = [sh for sh, c in shows.items() if c >= _GATE_MIN_LINES]
    eps = len({(r.get("episode_id") or r.get("label")) for r in tr})
    print(f"corpus: {len(files)} file(s), {len(tr)} ok lines, ~{eps} episode(s), "
          f"{len(substantial)} substantial show(s) of {len(shows)}"
          + (f"   (holdout '{holdout}' excluded)" if holdout else ""))
    for sh, c in shows.most_common():
        mark = "" if c >= _GATE_MIN_LINES else f"  (incidental <{_GATE_MIN_LINES} lines — ignored for the gate)"
        print(f"    {sh[:40]:40s} {c} lines{mark}")
    if len(substantial) < _GATE_MIN_SHOWS:
        print(f"  !! BELOW the measurement gate: {len(substantial)} substantial show(s), need "
              f">={_GATE_MIN_SHOWS} (+ >=2 genres / >=5 episodes / 1 held out).")
        print("     This run is WIRING VALIDATION ONLY, not evidence.")
    print()


def _divergence(rates: list) -> str | None:
    """>2x spread across shows (or any show at 0 while another >0) => defer."""
    live = [r for r in rates if r is not None]
    if len(live) < 2:
        return None
    hi, lo = max(live), min(live)
    if (lo == 0 and hi > 0) or (lo > 0 and hi / lo > 2):
        return f"{hi:.0%} vs {lo:.0%}"
    return None


def _drift_vote(sess, url: str, model: str, window: list[dict],
                provider: str = "groq", temperature: float = 0.0) -> dict:
    """One annotation pass -> {category: (worst_severity, scope)}; raises on failure."""
    lines = []
    for w in window:
        tag = ">> TARGET" if w["is_target"] else "line     "
        lines.append(f"{tag} zh: {w['src']}   en: {w['en']}")
    user = "WINDOW (earliest first):\n" + "\n".join(lines) + "\n\nAnnotate only the >> TARGET line."
    content = complete(
        sess, url, model,
        [{"role": "system", "content": _SYS}, {"role": "user", "content": user}],
        provider=provider, response_format={"type": "json_object"}, max_tokens=500,
        temperature=temperature, timeout=40,
    )
    data = json.loads(content)
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
    sess, url, model, provider, has_key = judge_session(os.getenv("GROQ_DRIFT_MODEL") or None)
    if not has_key:
        need = "OPENROUTER_API_KEY" if provider == "openrouter" else "GROQ_API_KEY"
        print(f"drift judge needs {need} for JUDGE_PROVIDER={provider} (repo-root .env).", file=sys.stderr)
        return
    votes = max(1, int(os.getenv("GROQ_DRIFT_VOTES", "3")))
    voter_models, temp = voters(model, votes)   # JUDGE_MODELS -> panel; else N votes of `model`
    maj = len(voter_models) // 2 + 1
    panel = len(set(voter_models)) > 1
    if any("qwen" in m.lower() for m in voter_models):
        print("WARNING: a grader is a Qwen model — same family as the translator (self-eval bias).", file=sys.stderr)
    reset_usage()

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
    grader = " + ".join(voter_models) if panel else f"{model} x{len(voter_models)}"
    print(f"  {len(sample)} of {len(targets)} eligible windows, grader={grader} via {provider}")
    print(f"  ~{len(sample)*len(voter_models)} calls, offline. Judge-flagged, NOT verified — confirm by hand.\n")

    # Accumulate per SHOW (never collapse straight to aggregate).
    stats: dict = collections.defaultdict(lambda: {
        "judged": 0, "flagged": 0, "cats": collections.Counter(),
        "scopes": collections.Counter(), "sevs": collections.Counter()})
    confirmed_rows = []
    for ep, i in sample:
        show = _show_key(ep[i].get("label", ""))
        window = _window(ep, i, shown)
        catcount = collections.Counter()
        worst: dict = {}
        scopes: dict = collections.defaultdict(collections.Counter)
        ran = 0
        for vm in voter_models:
            try:
                v = _drift_vote(sess, url, vm, window, provider, temp)
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
        stats[show]["judged"] += 1
        conf = {}
        for c, cnt in catcount.items():
            if cnt >= maj:
                sc = scopes[c].most_common(1)[0][0] if scopes[c] else None
                conf[c] = {"severity": worst[c], "source_scope": sc}
        if conf:
            st = stats[show]
            st["flagged"] += 1
            for c, d in conf.items():
                st["cats"][c] += 1
                st["sevs"][d["severity"]] += 1
                if c in _SCOPE_CATS and d["source_scope"]:
                    st["scopes"][d["source_scope"]] += 1
            tgt = ep[i]
            confirmed_rows.append({
                "show": show, "episode_id": tgt.get("episode_id"), "frame_id": tgt.get("frame_id"),
                "video_time": tgt.get("video_time"), "line_seq": tgt.get("line_seq"),
                "target_zh": tgt.get("source_text"),
                "target_en": window[[w["is_target"] for w in window].index(True)]["en"],
                "window": [{"zh": w["src"], "en": w["en"], "target": w["is_target"]} for w in window],
                "confirmed": conf, "votes": votes, "verified": None,  # <- the human fills this in
            })

    _report_drift(dict(stats), confirmed_rows, stratified, out_path)
    ce = cost_estimate()
    if ce:
        print(ce)


def _report_drift(stats: dict, confirmed_rows: list, stratified: bool, out_path: Path) -> None:
    """Pure reporting (no API) — per show first, then aggregate, with a >2x
    divergence -> DEFER guard and a below-gate 'not evidence' banner."""
    judged = sum(s["judged"] for s in stats.values())
    flagged = sum(s["flagged"] for s in stats.values())
    tag = "SHAPE — not the gate" if stratified else "GATE — verify first"
    print(f"windows with >=1 majority-confirmed drift error: {flagged}/{max(1, judged)} "
          f"({100*flagged//max(1, judged)}%)   [{tag}]\n")

    shows = sorted(stats.keys(), key=lambda k: -stats[k]["judged"])
    print("per show (a single-show number is NOT evidence — report before aggregate):")
    rates = []
    for sh in shows:
        s = stats[sh]
        r = s["flagged"] / s["judged"] if s["judged"] else None
        thin = "" if s["judged"] >= _MIN_SHOW_SAMPLE else f"  (<{_MIN_SHOW_SAMPLE} — too thin to weigh)"
        rates.append((s["judged"], r))
        print(f"    {sh[:34]:34s} {s['flagged']}/{s['judged']}  {f'{round(100*r)}%' if r is not None else '—'}{thin}")
    reliable = [r for j, r in rates if j >= _MIN_SHOW_SAMPLE and r is not None]
    div = _divergence(reliable)
    if div:
        print(f"\n  ** DEFER: per-show drift rates diverge >2x ({div}) — gate on broader corpus, park nothing. **")
    elif len(reliable) >= 2:
        print("\n  per-show rates within 2x — consistent so far.")
    if len(reliable) < _GATE_MIN_SHOWS:
        print(f"\n  !! BELOW MEASUREMENT GATE ({len(reliable)} show(s) with >={_MIN_SHOW_SAMPLE} judged) — "
              "WIRING VALIDATION ONLY, not evidence.")

    cat = collections.Counter(); sev = collections.Counter(); scope = collections.Counter()
    for s in stats.values():
        cat.update(s["cats"]); sev.update(s["sevs"]); scope.update(s["scopes"])
    if cat:
        print("\nconfirmed categories (share of judged windows, aggregate):")
        for c, cnt in cat.most_common():
            print(f"    {cnt:4d}  {100*cnt//max(1, judged):3d}%  {c}")
        print(f"  severity: {dict(sev)}")
        print(f"  leak/referent source_scope: {dict(scope)}   <- scene-level => Idea-3; immediate-neighbor => arbiter")

    if confirmed_rows:
        with out_path.open("w", encoding="utf-8") as f:
            for r in confirmed_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nconfirmed instances -> {out_path}  (set \"verified\": true/false per line)\n")
        for r in confirmed_rows[:12]:
            errs = ", ".join(f"{c}:{d['severity']}" + (f"/{d['source_scope']}" if d['source_scope'] else "")
                             for c, d in r["confirmed"].items())
            vt = r.get("video_time")
            loc = f"t={vt:.1f}s " if isinstance(vt, (int, float)) else ""
            print(f"  [{r['show'][:20]}] {loc}[{errs}]")
            for w in r["window"]:
                print(f"    {'>>' if w['target'] else '  '} {w['zh']}  ->  {w['en']}")
            print()
    else:
        print("\nno confirmed drift in the sample.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="*", help="log file(s) (default: most recent run)")
    ap.add_argument("--all", action="store_true", help="every run in the log dir (the multi-episode corpus)")
    ap.add_argument("--holdout", metavar="SUBSTR",
                    help="exclude lines whose label contains SUBSTR (reserve a validation episode)")
    ap.add_argument("--sample", type=int, default=60, help="target windows to judge (default 60)")
    ap.add_argument("--stratified", action="store_true", help="oversample risky windows (SHAPE, not the gate)")
    ap.add_argument("--votes", type=int, help="votes per window (else GROQ_DRIFT_VOTES or 3)")
    args = ap.parse_args()
    if args.votes:
        os.environ["GROQ_DRIFT_VOTES"] = str(args.votes)

    rows, files = _corpus(args.path, args.all, args.holdout)
    if not rows:
        print(f"no log rows found in {LOG_DIR}.", file=sys.stderr)
        return 1
    _corpus_banner(rows, files, args.holdout)
    suffix = "stratified" if args.stratified else "random"
    base = files[0] if files else (LOG_DIR / "corpus")
    out_path = base.with_suffix(f".drift-{suffix}.verify.jsonl")
    run(rows, args.sample, args.stratified, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
