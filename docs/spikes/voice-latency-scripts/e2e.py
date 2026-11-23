"""End-to-end spoken-turn latency, measured as one chained run per sample:
audio complete -> faster-whisper transcript -> streamed LLM reply -> first sentence
-> piper audio for that sentence ready to play. Endpointing (how long the VAD waits
for silence before calling the utterance complete) is NOT included; add it on top.

Usage: python scripts/e2e.py <stt-model> <base_url> <model> [--runs N]  (key: $BENCH_API_KEY)
"""
import argparse
import json
import os
import re
import time

import httpx
import soundfile as sf
from faster_whisper import WhisperModel
from piper import PiperVoice

from common import AUDIO, MODELS, RESULTS, summarize

SYSTEM = (
    "You are Kelp, a small desktop companion. You speak out loud, so answer in one or two short "
    "spoken sentences, no lists, no markdown. Temperament: dry, a little teasing, kind underneath."
)
SENT_END = re.compile(r"[.!?](\s|$)")
ap = argparse.ArgumentParser()
ap.add_argument("stt")
ap.add_argument("base")
ap.add_argument("model")
ap.add_argument("--clip", default="long8s")
ap.add_argument("--runs", type=int, default=5)
args = ap.parse_args()

stt = WhisperModel(args.stt, device="cpu", compute_type="int8", cpu_threads=4)
voice = PiperVoice.load(str(MODELS / "en_US-lessac-medium.onnx"))
audio = sf.read(AUDIO / f"{args.clip}.wav", dtype="float32")[0]
extra = json.loads(os.environ.get("BENCH_EXTRA", "{}"))
headers = {"Authorization": "Bearer " + os.environ["BENCH_API_KEY"]}


def turn(client, i):
    t0 = time.perf_counter()
    segs, _ = stt.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False,
                             without_timestamps=True)
    text = " ".join(s.text.strip() for s in segs)
    t_stt = time.perf_counter()
    reply = ""
    t_sent = None
    body = {"model": args.model, "stream": True, "max_tokens": 120, "temperature": 0.7,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": f"(message {i}) {text}"}], **extra}
    with client.stream("POST", f"{args.base}/chat/completions", headers=headers, json=body) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            ch = json.loads(line[5:]).get("choices") or [{}]
            reply += (ch[0].get("delta") or {}).get("content") or ""
            m = SENT_END.search(reply)
            if m:
                t_sent = time.perf_counter()
                first = reply[: m.end()].strip()
                break
    if t_sent is None:
        t_sent, first = time.perf_counter(), reply.strip()
    next(iter(voice.synthesize(first)))
    t_audio = time.perf_counter()
    return {"stt": t_stt - t0, "llm_first_sentence": t_sent - t_stt, "tts_first": t_audio - t_sent,
            "total": t_audio - t0, "transcript": text, "first_sentence": first}


with httpx.Client(timeout=120) as client:
    runs = [turn(client, i) for i in range(args.runs)]
out = {"stt": args.stt, "model": args.model, "clip": args.clip,
       **{k: summarize([r[k] for r in runs]) for k in ("stt", "llm_first_sentence", "tts_first", "total")},
       "samples": runs}
print(f"e2e {args.stt} + {args.model} + piper on {args.clip}: total median={out['total']['median']:.2f}s "
      f"p90={out['total']['p90']:.2f}s (stt {out['stt']['median']:.2f} + llm {out['llm_first_sentence']['median']:.2f} "
      f"+ tts {out['tts_first']['median']:.2f}) | {runs[-1]['first_sentence']!r}", flush=True)
RESULTS.mkdir(exist_ok=True)
(RESULTS / f"e2e_{args.stt}_{args.model.replace('/', '_')}_{args.clip}.json").write_text(json.dumps(out, indent=2))
