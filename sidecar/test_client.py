"""Standalone exercise of the sidecar — no browser, no extension.

Sends the contract's request shape for several scenarios and checks the
response, so we can prove the round-trip and the OCR-vote/dedup/gating logic
before the extension is ever wired up (that's session 3).

Usage: start the server, then `python test_client.py`.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"

# A valid 1x1 transparent PNG. Mock OCR ignores the pixels (real PaddleOCR will
# read actual subtitle crops here); we only need well-formed base64 frames.
PNG_1x1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
EMPTY = ""  # decodes to 0 bytes -> mock models a silent/empty box

passed = failed = 0


def post(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"FAIL   {name}  {detail}")


def main() -> int:
    try:
        with urllib.request.urlopen(BASE + "/health") as r:
            health = json.loads(r.read())
    except urllib.error.URLError:
        print(f"Cannot reach {BASE} — start the server first:")
        print("  cd sidecar && .venv/bin/python -m uvicorn app:app --port 8000")
        return 2
    print(f"health: {health}\n")

    three = [PNG_1x1, PNG_1x1, PNG_1x1]

    # 1. Normal line -> ok. The 3rd frame carries a planted single-glyph
    #    misread; the per-character majority vote must overrule it back to the
    #    clean line.
    code, r = post("/translate", {"frames": three, "frame_id": 1, "last_shipped_text": ""})
    print("normal:   ", r)
    check("normal -> 200", code == 200, str(code))
    check("normal -> status ok", r.get("status") == "ok", r.get("status"))
    check("vote overruled misread -> clean line", r.get("source_text") == "你是怎么忍住不娶的呢", r.get("source_text"))
    check("translation present + tagged mock", r.get("translation", "").startswith("[en·mock]"), r.get("translation"))
    check("confidence is mean of reads (~0.913)", abs(r.get("confidence", 0) - 0.913) < 0.01, str(r.get("confidence")))
    voted = r.get("source_text", "")

    # 2. Same line still on screen -> duplicate, no re-translation.
    code, r = post("/translate", {"frames": three, "frame_id": 2, "last_shipped_text": voted})
    print("duplicate:", r)
    check("duplicate -> status duplicate", r.get("status") == "duplicate", r.get("status"))
    check("duplicate flag true", r.get("duplicate") is True, str(r.get("duplicate")))
    check("duplicate keeps source_text (invariant 2)", r.get("source_text") == voted, r.get("source_text"))
    check("duplicate did not translate", r.get("translation", "") == "", r.get("translation"))

    # 3. Silent/empty box -> no_text.
    code, r = post("/translate", {"frames": [EMPTY, EMPTY, EMPTY], "frame_id": 3, "last_shipped_text": ""})
    print("silence:  ", r)
    check("silence -> status no_text", r.get("status") == "no_text", r.get("status"))
    check("silence -> empty translation", r.get("translation", "") == "", r.get("translation"))

    # 4. Contract enforcement: too many frames -> 422 (not a silent shrug).
    code, r = post("/translate", {"frames": [PNG_1x1] * 4, "frame_id": 4})
    check("4 frames -> 422 (contract enforced)", code == 422, str(code))

    # 5. Contract enforcement: unknown field -> 422 (extra='forbid').
    code, r = post("/translate", {"frames": three, "frame_id": 5, "bogus": 1})
    check("unknown field -> 422 (no silent drift)", code == 422, str(code))

    # 6. frame_id echoes back unchanged (drop-don't-queue depends on it).
    code, r = post("/translate", {"frames": three, "frame_id": 4821, "last_shipped_text": ""})
    check("frame_id echoed for staleness check", r.get("frame_id") == 4821, str(r.get("frame_id")))

    # 7. context_lines crosses the contract (mock tags how many it received).
    code, r = post("/translate", {
        "frames": three, "frame_id": 7, "last_shipped_text": "",
        "context_lines": ["我昨天", "去了图书馆"],
    })
    check("context_lines accepted", code == 200, str(code))
    check("mock reflects 2 context lines", "·ctx2]" in r.get("translation", ""), r.get("translation"))

    # 8. too many context_lines -> 422 (bounded per contract).
    code, r = post("/translate", {
        "frames": three, "frame_id": 8, "context_lines": ["a", "b", "c", "d", "e", "f"],
    })
    check("6 context_lines -> 422 (bounded)", code == 422, str(code))

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
