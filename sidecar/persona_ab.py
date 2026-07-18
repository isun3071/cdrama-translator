"""Persona A/B harness — build only; finalize the arms, then run.

Tests whether a persona reframe and/or two explicit clauses beat the current prompt,
as a clean 2x2 so persona and clauses are isolated (and their interaction visible):

              no extra clauses          + audience & commit clauses
  translator  control (= production)    clauses
  subtitler   persona                   persona+clauses

Design honesty:
- The shared RULE BLOCK is DERIVED from the production _system_prompt (strip its
  framing sentence), so every arm differs from control ONLY by the framing sentence
  and/or the two clauses — never by incidental rule drift. `control` IS production.
- The system prompt is overridden by seeding the translator's per-instance prompt
  cache (translator._sys[lang]) — NO production code is touched.
- Same paired input for every arm. Two strata: a RANDOM guardrail (regression check
  — did an arm make things worse?) and a BLIND targeted stratum of convention-bearing
  lines (name-tag / honorific / chengyu), selected by SOURCE PATTERN ONLY, never by
  whether the current prompt gets them right, with the matching rule recorded.
- Judged with GEMBA-MQM (reuse audit._mqm_vote), aggregated over votes, paired.

    python persona_ab.py --dry-run                 # sample + assemble arms, NO api calls
    python persona_ab.py [log ...] [--all] [--holdout SUBSTR] --guardrail 400 --targeted 80 --votes 3

CORPUS GATE: an A/B on ONE episode fits that episode. The report breaks results out
PER SHOW and flags an arm whose EFFECT flips sign across shows as DEFER (raw clean-
rate divergence between shows is expected and is not the trigger); below 3 shows it
banners the run as WIRING VALIDATION ONLY. Feed the broader corpus with multiple logs
or --all, and reserve a validation episode with --holdout.

The FRAMING and CLAUSES drafts below are placeholders — finalize them before a real
run. --dry-run prints all four assembled prompts so control-vs-arm differences are
inspectable.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from audit import load, split_stages, _latest_log, _mqm_vote, _MQM_WEIGHT
from audit_log import LOG_DIR
from glossary import GLOSSARY
from translate import _system_prompt, _LANG
from drift_judge import _show_key, _corpus, _corpus_banner, _MIN_SHOW_SAMPLE, _GATE_MIN_SHOWS  # corpus scoping
from judge_llm import judge_session

# --- the arms: framing (Factor A) x clauses (Factor B) ---------------------- #
# DRAFT — finalize wording before running. Kept minimal; the rule block is shared.

_FRAME_TR = "You are translating live subtitles for a Chinese TV drama into {lang}."  # == production
_FRAME_SUB = (  # DRAFT persona
    "You are an experienced subtitler of Chinese TV dramas for {lang}-speaking viewers "
    "unfamiliar with Chinese language and culture. Lines arrive one at a time with no "
    "lookahead; commit decisively to each as it arrives."
)
_CLAUSES = (  # DRAFT — audience clause + commit clause (NOTE: no 'no fragments' — that fights 6a)
    "\nFor this viewer specifically:\n"
    "- Your viewer cannot read hanzi or follow Chinese conventions: render the meaning "
    "for them, converting Chinese-specific conventions rather than preserving them, and "
    "never leave any Chinese untranslated.\n"
    "- Commit to what has arrived. Do not wait for, or invent, context that has not yet "
    "appeared; translate this line as it stands."
)


def _base_rules(lang: str) -> str:
    """The production rule block with its framing sentence stripped, so arms share it."""
    full = _system_prompt(lang)
    frame = _FRAME_TR.format(lang=lang) + " "
    return full[len(frame):] if full.startswith(frame) else full


def _arm_prompt(arm: str, lang: str) -> str:
    frame = _FRAME_SUB if arm in ("persona", "persona+clauses") else _FRAME_TR
    if arm == "control":
        return _system_prompt(lang)  # production verbatim
    prompt = frame.format(lang=lang) + " " + _base_rules(lang)
    if arm in ("clauses", "persona+clauses"):
        prompt += _CLAUSES
    return prompt


ARMS = ("control", "clauses", "persona", "persona+clauses")

# --- blind convention detectors (source-pattern only) ----------------------- #
_SURNAMES = ("王李张刘陈杨赵黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹袁邓许傅沈曾彭"
             "吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦"
             "江史顾侯邵孟龙万段钱汤尹黎易常武乔贺赖龚文")
_NAMETAG = re.compile(r"我[" + _SURNAMES + r"][一-鿿]{1,2}")
_HONORIFIC = re.compile(r"您|大人|老爷|公子|小姐|先生|阁下|姑娘|少爷|夫人|娘娘|陛下|殿下|王爷|大侠|恩公|前辈")
_CHENGYU = set(
    "名副其实 全力以赴 一言为定 迫不及待 语重心长 兴高采烈 迫在眉睫 理所当然 斩钉截铁 半途而废 "
    "义无反顾 不知所措 情不自禁 光明正大 恍然大悟 天经地义 微不足道 络绎不绝 千方百计 无济于事 "
    "息息相关 齐心协力 举世瞩目 一如既往 名正言顺 顺其自然 别无选择 一败涂地 深思熟虑 不由自主".split()
)


def _convention(s: str) -> str | None:
    """Which convention (if any) this SOURCE line carries — blind to any output."""
    if _NAMETAG.search(s):
        return "name-tag"
    if _HONORIFIC.search(s):
        return "honorific"
    if any(s[i:i + 4] in _CHENGYU for i in range(len(s) - 3)):
        return "chengyu"
    return None


def _sample(rows: list[dict], n: int, targeted: bool) -> list[dict]:
    ok = [r for r in rows if r.get("status") == "ok" and r.get("source_text")]
    if targeted:
        pool = []
        for r in ok:
            rule = _convention(r["source_text"])
            if rule:
                pool.append({**r, "_match_rule": rule})
    else:
        pool = [{**r, "_match_rule": None} for r in ok]
    if not pool:
        return []
    step = max(1, len(pool) // n)
    return pool[::step][:n]


# --- translation via prompt-cache seeding (no production change) ------------- #
def _make_arm_translators(lang: str):
    from translate import GroqTranslator
    key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
    arms = {}
    for arm in ARMS:
        t = GroqTranslator(key, model)
        t._sys[lang] = _arm_prompt(arm, lang)  # seed the cache -> overrides the system prompt
        arms[arm] = t
    return arms


def _score(sess, url, model, provider, votes, temperature, ctx, src, en):
    """Aggregate GEMBA-MQM over votes -> (median penalty, {confirmed cat: sev})."""
    maj = votes // 2 + 1
    catcount, worst, penalties = collections.Counter(), {}, []
    for _ in range(votes):
        try:
            catsev, _notes = _mqm_vote(sess, url, model, ctx, src, en, provider, temperature)
        except Exception:
            continue
        penalties.append(sum(_MQM_WEIGHT[s] for s in catsev.values()))
        for c, s in catsev.items():
            catcount[c] += 1
            if c not in worst or _MQM_WEIGHT[s] > _MQM_WEIGHT[worst[c]]:
                worst[c] = s
    if not penalties:
        return None, {}
    return statistics.median(penalties), {c: worst[c] for c, cnt in catcount.items() if cnt >= maj}


def run(rows: list[dict], n_guard: int, n_targeted: int, votes: int, target_lang: str, out_path: Path) -> None:
    if not os.getenv("GROQ_API_KEY", "").strip():
        print("run needs GROQ_API_KEY (the arms translate via Groq).", file=sys.stderr)
        return
    lang = _LANG.get(target_lang, target_lang)
    sess, url, judge_model, provider, has_key = judge_session()
    if not has_key:
        need = "OPENROUTER_API_KEY" if provider == "openrouter" else "GROQ_API_KEY"
        print(f"judge needs {need} for JUDGE_PROVIDER={provider}.", file=sys.stderr)
        return
    temp = 0.4 if votes > 1 else 0.0
    translators = _make_arm_translators(lang)

    guard = [{**r, "_stratum": "guardrail"} for r in _sample(rows, n_guard, targeted=False)]
    targ = [{**r, "_stratum": "targeted"} for r in _sample(rows, n_targeted, targeted=True)]
    items = guard + targ
    calls = len(items) * len(ARMS) * (1 + votes)
    print(f"\npersona A/B — {len(guard)} guardrail + {len(targ)} targeted lines x {len(ARMS)} arms")
    print(f"  translate model={os.getenv('GROQ_MODEL','qwen/qwen3.6-27b')}  judge={judge_model} via {provider} x{votes} votes")
    print(f"  ~{calls} API calls. Paired; McNemar power needs ~30 discordant pairs.\n")

    results = []
    for it in items:
        src = it["source_text"]
        ctx_list = it.get("context_lines") or []
        ctx = "\n".join(ctx_list)
        gloss = GLOSSARY.matching(src, ctx_list, it.get("label", ""))
        row = {"stratum": it["_stratum"], "show": _show_key(it.get("label", "")),
               "match_rule": it.get("_match_rule"),
               "frame_id": it.get("frame_id"), "source_text": src, "arms": {}}
        for arm, t in translators.items():
            en = t.translate(src, "ch", target_lang, ctx_list, it.get("continuation", False), gloss)
            pen, conf = _score(sess, url, judge_model, provider, votes, temp, ctx, src, en)
            row["arms"][arm] = {"translation": en, "penalty": pen, "confirmed": conf}
        results.append(row)

    _report(results)
    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nper-line outputs -> {out_path}")


def _arm_summary(sub: list[dict]) -> dict:
    out = {}
    for arm in ARMS:
        scored = [r["arms"][arm] for r in sub if r["arms"].get(arm, {}).get("penalty") is not None]
        if scored:
            clean = sum(1 for a in scored if not a["confirmed"])
            out[arm] = (clean, len(scored), statistics.mean(a["penalty"] for a in scored))
    return out


def _mcnemar(sub: list[dict], arm: str) -> tuple[int, int]:
    b = c = 0
    for r in sub:
        ac, cc = r["arms"].get(arm), r["arms"].get("control")
        if not ac or not cc or ac["penalty"] is None or cc["penalty"] is None:
            continue
        a_clean, c_clean = not ac["confirmed"], not cc["confirmed"]
        if a_clean and not c_clean:
            b += 1
        elif c_clean and not a_clean:
            c += 1
    return b, c


def _report(results: list[dict]) -> None:
    for stratum in ("guardrail", "targeted"):
        sub = [r for r in results if r["stratum"] == stratum]
        if not sub:
            continue
        shows = sorted({r["show"] for r in sub}, key=lambda s: -sum(1 for r in sub if r["show"] == s))
        print(f"=== {stratum} (n={len(sub)}, {len(shows)} show(s)) ===")

        # Per show: arm clean% and the arm's DELTA vs control. The A/B generalization
        # test is whether an arm's EFFECT keeps its sign across shows — raw clean-rate
        # divergence between shows is expected (shows differ in difficulty) and is NOT
        # the defer trigger; a sign flip in the effect is.
        print("  per show — arm clean% (delta vs control):")
        arm_signs = {a: set() for a in ARMS[1:]}
        reliable_shows = 0
        for sh in shows:
            ss = [r for r in sub if r["show"] == sh]
            summ = _arm_summary(ss)
            crate = (summ["control"][0] / summ["control"][1]) if "control" in summ else None
            reliable = len(ss) >= _MIN_SHOW_SAMPLE
            reliable_shows += 1 if reliable else 0
            parts = []
            for a in ARMS:
                if a not in summ:
                    continue
                cl, n, _pen = summ[a]
                rate = cl / n
                if a == "control" or crate is None:
                    parts.append(f"{a}={round(100*rate)}%")
                else:
                    d = rate - crate
                    parts.append(f"{a}={round(100*rate)}%({'+' if d >= 0 else ''}{round(100*d)})")
                    if reliable:  # a thin show can't cast a sign vote
                        arm_signs[a].add(1 if d > 0 else (-1 if d < 0 else 0))
            thin = "" if reliable else f"  (<{_MIN_SHOW_SAMPLE}, not weighed)"
            print(f"    {sh[:24]:24s} " + "  ".join(parts) + thin)

        summ = _arm_summary(sub)
        print("  aggregate:")
        for a in ARMS:
            if a in summ:
                cl, n, pen = summ[a]
                print(f"    {a:16s} clean {cl}/{n} ({100*cl//n}%)   penalty {pen:.2f}")

        print("  paired vs control (arm better : control better; ~30 discordant for power):")
        for a in ARMS[1:]:
            b, c = _mcnemar(sub, a)
            flag = "" if (b + c) >= 30 else "  (underpowered <30)"
            gen = "  ** effect flips sign across shows -> DEFER **" if len(arm_signs[a] - {0}) > 1 else ""
            print(f"    {a:16s} {b} : {c}{flag}{gen}")

        if stratum == "targeted":
            print("  addition-category incidence (name/convention-leak proxy):")
            for a in ARMS:
                add = sum(1 for r in sub if "accuracy/addition" in r["arms"].get(a, {}).get("confirmed", {}))
                print(f"    {a:16s} {add}/{len(sub)}")

        if reliable_shows < _GATE_MIN_SHOWS:
            print(f"  !! {reliable_shows} show(s) with >={_MIN_SHOW_SAMPLE} lines — WIRING VALIDATION ONLY, not evidence.")
        print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="*", help="log file(s) (default: most recent run)")
    ap.add_argument("--all", action="store_true", help="every run in the log dir (multi-episode corpus)")
    ap.add_argument("--holdout", metavar="SUBSTR",
                    help="exclude lines whose label contains SUBSTR (reserve a validation episode)")
    ap.add_argument("--guardrail", type=int, default=400, help="random regression-guardrail lines")
    ap.add_argument("--targeted", type=int, default=80, help="blind convention-bearing lines")
    ap.add_argument("--votes", type=int, default=3, help="GEMBA-MQM votes per output")
    ap.add_argument("--lang", default="en", help="target_lang")
    ap.add_argument("--dry-run", action="store_true", help="sample + assemble arms, NO api calls")
    args = ap.parse_args()

    rows, files = _corpus(args.path, args.all, args.holdout)
    if not rows:
        print(f"no log rows found in {LOG_DIR}.", file=sys.stderr)
        return 1
    lang = _LANG.get(args.lang, args.lang)
    _corpus_banner(rows, files, args.holdout)

    if args.dry_run:
        print("(DRY RUN — no API calls)\n")
        guard = _sample(rows, args.guardrail, targeted=False)
        targ = _sample(rows, args.targeted, targeted=True)
        print(f"guardrail sample: {len(guard)} random ok lines")
        print(f"targeted sample:  {len(targ)} convention-bearing lines (blind, by source rule)")
        print(f"  by matched rule: {dict(collections.Counter(r['_match_rule'] for r in targ))}")
        print(f"  by show: {dict(collections.Counter(_show_key(r.get('label','')) for r in targ))}")
        for r in targ[:8]:
            print(f"    [{r['_match_rule']:9s}] {r['source_text']}")
        print("\n--- assembled arm prompts (finalize DRAFTs before a real run) ---")
        for arm in ARMS:
            p = _arm_prompt(arm, lang)
            print(f"\n### {arm} ({len(p)} chars) ###\n{p}")
        print("\n(DRY RUN complete — no lines translated, no judge calls made.)")
        return 0

    base = files[0] if files else (LOG_DIR / "corpus")
    out_path = base.with_suffix(".persona-ab.jsonl")
    run(rows, args.guardrail, args.targeted, args.votes, args.lang, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
