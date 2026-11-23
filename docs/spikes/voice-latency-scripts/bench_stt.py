"""faster-whisper CPU int8 benchmark: wall time from 'audio complete' to final transcript + WER.

Usage: python bench_stt.py <model> [--threads N] [--beam N] [--warm N]
Model is a faster-whisper size name (tiny.en, base.en, small.en) or an HF repo id.
Each invocation is a fresh process, so the first call is a true cold start (model
already on disk after the first download; page-cache effects are not controlled).
"""
import argparse
import json
import re
import time

import jiwer
import soundfile as sf
from faster_whisper import WhisperModel

from common import AUDIO, RESULTS, UTTERANCES, summarize

ap = argparse.ArgumentParser()
ap.add_argument("model")
ap.add_argument("--threads", type=int, default=4)
ap.add_argument("--beam", type=int, default=1)
ap.add_argument("--warm", type=int, default=7)
args = ap.parse_args()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", s.lower())).strip()


t0 = time.perf_counter()
model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=args.threads)
load_s = time.perf_counter() - t0

clips = {k: sf.read(AUDIO / f"{k}.wav", dtype="float32")[0] for k in UTTERANCES}


def run(audio):
    t = time.perf_counter()
    segs, _ = model.transcribe(
        audio, language="en", beam_size=args.beam, vad_filter=False,
        condition_on_previous_text=False, without_timestamps=True,
    )
    text = " ".join(s.text.strip() for s in segs)  # generator is lazy: decoding happens here
    return time.perf_counter() - t, text


out = {"model": args.model, "threads": args.threads, "beam": args.beam, "load_s": load_s, "clips": {}}
for name, audio in clips.items():
    first_s, text = run(audio)
    warm = []
    for _ in range(args.warm):
        s, text = run(audio)
        warm.append(s)
    wer = jiwer.wer(norm(UTTERANCES[name]), norm(text))
    out["clips"][name] = {
        "audio_s": len(audio) / 16000, "first_s": first_s, "warm": summarize(warm),
        "warm_samples": warm, "wer": wer, "transcript": text,
    }
    print(f"{args.model} t={args.threads} b={args.beam} {name}: first={first_s:.3f}s "
          f"warm median={out['clips'][name]['warm']['median']:.3f}s p90={out['clips'][name]['warm']['p90']:.3f}s "
          f"WER={wer:.3f} | {text!r}", flush=True)

RESULTS.mkdir(exist_ok=True)
tag = args.model.replace("/", "_")
(RESULTS / f"stt_{tag}_t{args.threads}_b{args.beam}.json").write_text(json.dumps(out, indent=2))
