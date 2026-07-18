# sidecar — local FastAPI service (Shape B)

The local companion process that owns OCR, the 3-frame majority vote, dedup, and
translation. It runs on `127.0.0.1` and answers the `POST /translate` contract
from `CLAUDE.md`. The extension never learns what's behind the endpoint — that's
the whole point of the seam, and it's how Shape B (this Python process) later
becomes Shape A (in-browser wasm) without the extension changing.

**This is a local process, not a hosted/cloud server.** Nothing you deploy,
nothing with users. In session 2 it does *not* even translate for real
(translation is mocked) and makes no network calls at all.

## Status: session 3 — wired to the extension, mock OCR/translation

The endpoint, the Pydantic contract models, the per-character majority vote,
dedup, confidence/consensus gating, CORS, and the swappable OCR/translation
seams are all in and tested end-to-end, and the extension now calls this service
for real (via its background script). The `context_lines` field (last 2-3
source hanzi lines, DOCUMENTATION.md 6a/6b) is in the contract and flows through
to the `Translator`; the mock tags its output `[lang·mock·ctxN]` so you can see
it crossing the seam. **OCR and translation are still mocks:** the mock returns
one of a few canned lines per real crop (so the overlay changes per subtitle).

Deferred to the real-translation build (they need a real model to judge
grammatical continuation and produce pair translations, DOCUMENTATION.md 6a):
**context-aware decoding** (weaving `context_lines` into the prompt) and
**re-translation / screen-rewriting** (revise the overlay when a new line
completes the previous, with tail-masking). The plumbing is in place so they
slot in without touching the contract. Next step swaps the mock for real
PaddleOCR; translation stays mocked until the Groq build.

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
