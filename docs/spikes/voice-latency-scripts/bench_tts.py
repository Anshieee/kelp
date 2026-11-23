"""TTS latency: time from reply text to the first playable audio chunk, plus the
real-time factor (synthesis time / audio length). CPU only, no speakers.

Usage: python scripts/bench_tts.py piper|kokoro [--threads N] [--runs N]
"""
import argparse
import json
import time

import numpy as np

from common import MODELS, RESULTS, summarize

FIRST = "Fine, take five minutes, you have been staring at that test long enough."
REPLY = FIRST + " When you are back, paste the failure and we will look at it together."

ap = argparse.ArgumentParser()
ap.add_argument("engine", choices=["piper", "kokoro"])
ap.add_argument("--threads", type=int, default=4)
ap.add_argument("--runs", type=int, default=6)
args = ap.parse_args()

t0 = time.perf_counter()
if args.engine == "piper":
    from piper import PiperVoice
    import onnxruntime as ort  # noqa: F401  (piper uses it internally)

    voice = PiperVoice.load(str(MODELS / "en_US-lessac-medium.onnx"))
    sr = voice.config.sample_rate

    def first_chunk(text):
        t = time.perf_counter()
        for c in voice.synthesize(text):  # one chunk per sentence
            return time.perf_counter() - t, len(c.audio_float_array) / sr

    def full(text):
        t = time.perf_counter()
        a = np.concatenate([c.audio_float_array for c in voice.synthesize(text)])
        return time.perf_counter() - t, len(a) / sr
else:
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = args.threads
    sess = ort.InferenceSession(str(MODELS / "kokoro-v1.0.int8.onnx"), opts, providers=["CPUExecutionProvider"])
    kok = Kokoro.from_session(sess, str(MODELS / "voices-v1.0.bin"))

    def first_chunk(text):
        t = time.perf_counter()
        a, sr = kok.create(text, voice="af_heart", speed=1.0, lang="en-us")
        return time.perf_counter() - t, len(a) / sr

    def full(text):
        t = time.perf_counter()
        a, sr = kok.create(text, voice="af_heart", speed=1.0, lang="en-us")
        return time.perf_counter() - t, len(a) / sr
load_s = time.perf_counter() - t0

cold_first, _ = first_chunk(FIRST)
firsts, rtfs = [], []
for _ in range(args.runs):
    s, _audio_s = first_chunk(FIRST)
    firsts.append(s)
    syn, dur = full(REPLY)
    rtfs.append(syn / dur)
out = {"engine": args.engine, "threads": args.threads, "load_s": load_s, "cold_first_sentence_s": cold_first,
       "warm_first_sentence": summarize(firsts), "rtf": summarize(rtfs)}
print(f"{args.engine}: load={load_s:.2f}s cold first-sentence={cold_first:.3f}s warm first-sentence median="
      f"{out['warm_first_sentence']['median']:.3f}s p90={out['warm_first_sentence']['p90']:.3f}s RTF median={out['rtf']['median']:.3f}", flush=True)
RESULTS.mkdir(exist_ok=True)
(RESULTS / f"tts_{args.engine}_t{args.threads}.json").write_text(json.dumps(out, indent=2))
