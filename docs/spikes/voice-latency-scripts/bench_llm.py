"""LLM latency for a spoken reply: time to first token (TTFT) and time to first
sentence end (TTFS, the point where TTS can start speaking).

Usage:
  python bench_llm.py ollama <model> [--runs N]
  python bench_llm.py openai <base_url> <model> [--runs N]   # key from $BENCH_API_KEY

Run 1 is reported separately as "first" (for ollama it includes model load when
the model is not resident). The rest are summarised as warm samples.
"""
import argparse
import json
import os
import re
import time

import httpx

from common import RESULTS, summarize

SYSTEM = (
    "You are Kelp, a small desktop companion. You speak out loud, so answer in one or two short "
    "spoken sentences, no lists, no markdown. Temperament: dry, a little teasing, kind underneath."
)
USER = "I have been working on this project all morning and I am a little stuck, so could you remind me to take a short break and then help me look at the failing test?"
SENT_END = re.compile(r"[.!?](\s|$)")

ap = argparse.ArgumentParser()
ap.add_argument("kind", choices=["ollama", "openai"])
ap.add_argument("target")
ap.add_argument("model", nargs="?")
ap.add_argument("--runs", type=int, default=6)
args = ap.parse_args()

messages: list = []


def set_messages(i):
    # A new user turn each run: the system prompt can stay in the KV cache (as it would in a
    # real session) but the user text must be evaluated fresh, so TTFT is not a cache hit.
    messages[:] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"(message {i}) " + USER}]


def stream_ollama(client, model):
    with client.stream("POST", f"http://{os.environ['OLLAMA_HOST']}/api/chat",
                       json={"model": model, "messages": messages, "stream": True,
                             "options": {"num_predict": 80, "temperature": 0.7}}) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                d = json.loads(line)
                yield d.get("message", {}).get("content", ""), d


def stream_openai(client, base, model):
    headers = {"Authorization": "Bearer " + os.environ["BENCH_API_KEY"]}
    with client.stream("POST", f"{base}/chat/completions", headers=headers,
                       json={"model": model, "messages": messages, "stream": True,
                             "max_tokens": 120, "temperature": 0.7,
                             **json.loads(os.environ.get("BENCH_EXTRA", "{}"))}) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            d = json.loads(data)
            ch = d.get("choices") or [{}]
            yield (ch[0].get("delta") or {}).get("content") or "", d


def one(client, i):
    set_messages(i)
    t0 = time.perf_counter()
    ttft = ttfs = None
    text = ""
    last = {}
    gen = stream_ollama(client, args.target) if args.kind == "ollama" else stream_openai(client, args.target, args.model)
    for piece, last in gen:
        if piece and ttft is None:
            ttft = time.perf_counter() - t0
        text += piece
        if ttfs is None and SENT_END.search(text):
            ttfs = time.perf_counter() - t0
    total = time.perf_counter() - t0
    tps = None
    if args.kind == "ollama" and last.get("eval_count") and last.get("eval_duration"):
        tps = last["eval_count"] / (last["eval_duration"] / 1e9)
    return {"ttft": ttft, "ttfs": ttfs if ttfs is not None else total, "total": total, "tps": tps, "text": text.strip()}


name = args.target if args.kind == "ollama" else args.model
with httpx.Client(timeout=300) as client:
    runs = [one(client, i) for i in range(args.runs)]
first, warm = runs[0], runs[1:]
out = {
    "kind": args.kind, "model": name, "first": first,
    "warm_ttft": summarize([r["ttft"] for r in warm]),
    "warm_ttfs": summarize([r["ttfs"] for r in warm]),
    "warm_tps": [r["tps"] for r in warm], "samples": runs,
}
print(f"{args.kind} {name}: first ttft={first['ttft']:.2f}s | warm ttft median={out['warm_ttft']['median']:.2f}s "
      f"p90={out['warm_ttft']['p90']:.2f}s | warm first-sentence median={out['warm_ttfs']['median']:.2f}s "
      f"| tps={warm[0]['tps']} | {warm[-1]['text'][:120]!r}", flush=True)
RESULTS.mkdir(exist_ok=True)
(RESULTS / f"llm_{args.kind}_{name.replace('/', '_').replace(':', '_')}.json").write_text(json.dumps(out, indent=2))
