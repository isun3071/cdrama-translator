# sidecar — local FastAPI service (Shape B)

The local companion process that owns OCR, the 3-frame majority vote, dedup, and
translation. It runs on `127.0.0.1` and answers the `POST /translate` contract
from `CLAUDE.md`. The extension never learns what's behind the endpoint — that's
the whole point of the seam, and it's how Shape B (this Python process) later
becomes Shape A (in-browser wasm) without the extension changing.

**This is a local process, not a hosted/cloud server.** Nothing you deploy,
nothing with users. In session 2 it does *not* even translate for real
(translation is mocked) and makes no network calls at all.

## Status: fully real pipeline (RapidOCR + Groq)

The endpoint, the Pydantic contract, the per-character majority vote, dedup,
confidence/consensus gating, CORS, and the swappable seams are all in and tested
end-to-end, and the extension calls this service for real. **OCR and translation
are both real now**; measured ~390-500ms full round-trips (3 real OCR frames +
vote + Groq), at the documented budget.

**OCR is real.** `RapidOcrEngine` (ocr.py) runs RapidOCR (ONNX Runtime + PP-OCR
models, strong on CJK — and the ONNX/PP-OCR pipeline the wasm Shape A will
mirror). `make_ocr()` uses it when importable, else the mock; `CDT_OCR=mock`
forces the mock. Installs clean on Python 3.14 (no paddlepaddle).

**Mask-before-OCR (§4) is on** (`mask_for_ocr`, `OCR_MASK=True`): the raw crop is
background-subtracted (near-white fill next to a dark stroke kept, rest → black)
before recognition, so moving clutter can't be read as a stray glyph. On
adversarial synthetic clutter this took exact reads from 2/6 → 6/6 with **no**
latency cost (~40ms/frame). Two landmines learned and encoded: a black *border*
on the masked image makes DBNet detection explode (~40ms → ~1200ms, so
`OCR_MASK_PAD=0`), and the confidence gate had to drop to 0.50 because short CJK
lines score low and masking made the gate — not clutter — the thing dropping
correct reads (走吧 @ 0.54).

**Translation is real now.** `GroqTranslator` (translate.py) calls Groq's
chat-completions API. `make_translator()` picks Groq when `GROQ_API_KEY` is set
(loaded from the repo-root `.env`), else the mock; `CDT_TRANSLATOR=mock` forces
the mock for offline work. Model is `GROQ_MODEL` (default
`llama-3.3-70b-versatile`). Measured ~140-285ms round-trips, well under budget.
The system prompt treats OCR as a noisy channel — tolerate character glitches,
don't invent — and preserves dramatic nuance (idiom/tone, no flattening).

**Split-sentence re-translation (6a) is on, with a two-layer speaker guard.** The
extension flags a *candidate* continuation from cheap signals — previous line had
no terminal punctuation **and no sentence-final particle** (吗/吧/呢/嘛, so a
question like 你爱我吗 doesn't bridge onto its answer) and the gap was short — then
holds/​revises the overlay in place. The service is the arbiter: it **defaults to
separate** (translate only the last line) and fuses into one sentence only on an
unmistakable mid-clause split (虽然…/如果…). So 虽然他非常努力 / 但最后还是失败了 →
*"Although he worked very hard, he still failed in the end,"* but 你到底爱不爱我 /
我爱你才怪 stays as just the reply.

Honest ceiling: without speaker labels, a genuinely ambiguous statement+objection
(这件事就这么定了 / 我不同意) will *occasionally* still fuse — inherent to text-only
speaker separation. The particle layer is deterministic; the arbiter is
best-effort; together they strictly beat the old fuse-whenever-punctuation-absent.
Context window stays at 2-3 lines on purpose: bigger doesn't help single-line
translation and pulls in more cross-speaker leak.

Deferred refinements (6a): **tail-masking** (commit the stable prefix so a
revision rarely rewrites read text) and **OCR-uncertainty passing** (hand the
model the per-position vote disagreements so it repairs look-alike substitutions
with information rather than guessing — see the caveat in the top-level README).

### Translation config (.env at repo root)

```
GROQ_API_KEY=gsk_...            # required for real translation; gitignored
GROQ_MODEL=llama-3.3-70b-versatile   # optional override
CDT_TRANSLATOR=mock             # optional: force the mock translator even with a key
CDT_OCR=mock                    # optional: force the mock OCR (offline / contract tests)
```

## Auditing translations

Every request is logged as one JSON line to `logs/translations.jsonl`
(gitignored), capturing the **whole pipeline per line** so a bad output can be
traced to the stage it went wrong — not just the final translation:

- `stage:"translate"` (sidecar): `video_time` (jump back to the exact frame),
  per-frame `reads` (raw hanzi + conf, **pre-vote**), voted `source_text` + mean
  `confidence`, `context_lines`, `continuation` (the ↳cont candidate flag),
  `translation`, `latency_ms`, `provider`/`model`, `status`, `label` (episode).
- `stage:"display"` (extension, reported back via `POST /log`): `visible_ms` and
  `outcome` (`expired` / `preempted` / `dropped` / `revised`), joined to the
  translate record by `frame_id` — this is how you catch a good translation that
  was shown too briefly or never shown.

Toggle/relocate with `CDT_LOG=0` / `CDT_LOG_PATH=...`.

Analyse with `audit.py`:

```
python audit.py                  # stats: status, OCR confidence, latency, display outcomes, per-episode
python audit.py --pipeline 20    # last 20 lines' full per-stage trace (reads -> voted -> ctx -> translation -> display)
python audit.py --judge 40       # LLM-judge a sample for accuracy (1-5 + flagged suspects)
python audit.py --label 四合院 --lang en --judge 40
```

The judge defaults to the same Groq model that produced the translations — a
self-eval first pass, good for surfacing suspects but biased high; set
`GROQ_JUDGE_MODEL` to a different/stronger model for a real audit.

```
sidecar/
  app.py            FastAPI app + POST /translate orchestration (the pipeline)
  contract.py       TranslateRequest / TranslateResponse — the contract as Pydantic
  ocr.py            OcrEngine seam + MockOcr (PaddleOcr drops in here)
  vote.py           per-character majority vote across the 3 frames + consensus
  dedup.py          string-similarity dedup vs last_shipped_text
  translate.py      Translator seam + MockTranslator (Groq/Ollama drop in here)
  config.py         thresholds (videocr-derived defaults)
  test_client.py    standalone HTTP exercise of every status path
  requirements.txt  fastapi, uvicorn, pydantic
  run.sh            launch helper
```

## Run it

```bash
cd sidecar
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh                       # or: .venv/bin/python -m uvicorn app:app --port 8000 --reload
```

- Interactive contract docs + a click-to-test UI: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

## Test it (standalone, no browser)

With the server running, in another terminal:

```bash
cd sidecar && .venv/bin/python test_client.py
```

It drives every path: a normal line (the vote corrects a planted single-frame
misread 要→娶), a duplicate (dedup, no re-translation), a silent box
(`no_text`), and two contract-enforcement cases (too many frames, unknown field
→ `422`). Expect `14 passed, 0 failed`.

## Contract (authoritative copy in `CLAUDE.md`)

```
POST /translate
{ "frames": ["<b64 png>", ...1-3], "source_lang": "ch", "target_lang": "en",
  "frame_id": 4821, "last_shipped_text": "<what the extension last displayed>",
  "context_lines": ["<prev source line>", ...last 2-3 hanzi lines] }

-> { "frame_id": 4821, "status": "ok|no_text|duplicate|low_confidence",
     "source_text": "<hanzi>", "translation": "<target>", "confidence": 0.94,
     "duplicate": false }
```

`extra="forbid"` on the request means an unrecognized field is a loud `422`, not
a silent drift — the extension and service can't fall out of contract unnoticed.

## Next step — real OCR, and a Python-version caveat

PaddleOCR + paddlepaddle is a heavy install (hundreds of MB of wheels + model
downloads). Note this repo's default `python3` is **3.14**, and paddle wheels
historically lag new Python releases — if `pip install paddlepaddle` finds no
3.14 wheel, we'll stand the sidecar's venv up on Python 3.11/3.12 instead (it's
behind the contract, so its runtime is a free choice). RapidOCR/ONNX is the
fallback OCR if paddle is uncooperative. The mock lets everything downstream —
including session 3's extension wiring — proceed regardless.
# cdrama-translator
