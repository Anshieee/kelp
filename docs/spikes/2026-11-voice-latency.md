# Voice-latency spike: results (2026-11-23)

Phase 0 spike for [`docs/PRODUCT_BRIEF.md`](../PRODUCT_BRIEF.md) section 10.

**Answer:** the ~1 s voice target in `docs/PRODUCT_BRIEF.md` is not reachable with a
cascaded STT → LLM → TTS pipeline on this laptop. The best measured chain is
**3.1 s median** from "the user stopped talking" to "first sentence of audio ready",
before endpointing. Local-only LLMs are 3–7 s to the first token on this CPU.

## Machine and conditions

- Intel Core i7-1255U (2P+8E cores, 12 threads), 15.3 GiB RAM, no discrete GPU, Linux/Wayland.
- Not idle: about 4 GiB RAM free and load average ~3.5 while measuring (browser, editor
  and agent processes running). That is realistic for a companion that runs next to your
  work, but a quiet machine would be somewhat faster. The chained run's STT step was
  1.4–1.6 s versus 0.87–0.98 s when STT ran on its own, which is contention.
- Test audio is synthetic speech (Piper `en_US-lessac-medium`, resampled to 16 kHz) with
  0.5 s of silence at each end: `short3s` (3.9 s total) and `long8s` (8.8 s total).
- Every number is from the spike's `results/*.json` (kept outside the repo; they hold model output text). The scripts are in [`voice-latency-scripts/`](voice-latency-scripts/); run them from that folder after `source env.sh` and `uv sync`, with models from Hugging Face (faster-whisper) and the Piper and Kokoro releases in `models/`. "Warm" means the model
  was already loaded; the first call is reported separately.

## Stage results

### Speech-to-text (faster-whisper, CPU int8, 4 threads, beam 1) — `bench_stt.py`

| Model | Clip | Warm median | p90 | WER |
|---|---|---|---|---|
| tiny.en | short3s | 0.87 s | 0.90 s | 0.0 |
| tiny.en | long8s | 0.98 s | 1.06 s | 0.0 |
| base.en | short3s | 1.72 s | 1.72 s | 0.0 |
| base.en | long8s | 1.93 s | 1.95 s | 0.0 |

WER is 0 on clean synthetic speech; real microphones, accents and background noise will
be worse, so tiny.en's accuracy is unproven. `small.en` was downloaded but not benchmarked.

### LLM, time to first sentence (where TTS can start) — `bench_llm.py`

Each run sends a new user message, so only the system prompt can be cached.

| Backend | Model | First call TTFT | Warm TTFT median | Warm first-sentence median |
|---|---|---|---|---|
| Ollama (local CPU) | phi4-mini 3.8B Q4_K_M | 18.6 s (load) | 3.17 s | 4.68 s |
| Ollama (local CPU) | qwen2.5 7B Q4_K_M | 24.0 s (load) | 7.01 s | 7.75 s |
| OpenRouter | anthropic/claude-haiku-5.5, reasoning off, `sort: latency` (see below) | 1.07 s | 1.04 s | 1.35 s |
| Kiro gateway :8000 | claude-haiku-5-5 | 18.8 s | 17.2 s | 17.2 s |
| Kiro gateway :8000 | gpt-5.6-luna | 14.5 s | 14.4 s | 14.5 s |

The OpenRouter rows were run with `BENCH_API_KEY` set in the shell (never committed) and
`BENCH_EXTRA='{"reasoning":{"enabled":false},"provider":{"sort":"latency"}}'`, which
`bench_llm.py` and `e2e.py` merge into the request body.

Local generation ran at 2.5–4.8 tokens/s. The Kiro gateway wraps an agent session per
request, so it is unusable for voice; it is listed only because it was tested.
`google/gemini-2.5-flash-lite` on OpenRouter returned 429 and was not measured.

### Text-to-speech, first sentence — `bench_tts.py`

| Engine | Warm first sentence median | p90 | Real-time factor |
|---|---|---|---|
| Piper en_US-lessac-medium | 0.455 s | 0.460 s | 0.11 |
| Kokoro v1.0 int8 (af_heart) | 3.60 s | 4.16 s | 0.87 |

Kokoro sounds better but is too slow on this CPU for the first sentence.

## End to end — `e2e.py` (tiny.en → OpenRouter Haiku 5.5 → Piper, chained, 5 runs)

| Clip | Total median | p90 | STT | LLM first sentence | TTS |
|---|---|---|---|---|---|
| short3s | 3.10 s | 3.16 s | 1.43 s | 1.20 s | 0.49 s |
| long8s | 3.28 s | 3.96 s | 1.56 s | 1.33 s | 0.39 s |

Not included: endpointing delay (typically 0.3–0.8 s of silence before deciding the user is
done), audio device latency, and network variance from other locations. Cost for all
OpenRouter calls in this spike: about $0.0003.

## What this means for the brief

1. **Change the target.** Use "visual acknowledgement in ≤ 200 ms, first spoken word in
   ≤ 2.5 s p50 on a laptop CPU in cloud-brain mode". The avatar's "listening"/"thinking"
   animation covers the gap; it costs nothing and is already a pet state.
2. **Biggest remaining win is STT.** Transcribing while the user speaks (streaming partials
   with VAD) removes most of the 0.9–1.5 s after speech ends. Not measured here.
3. **Local-only voice needs a GPU or NPU.** On this CPU a local 4B model is ~4.7 s to the
   first sentence. Local mode should default to a cloud model for voice when one is
   configured, and say so, or offer text-only.
4. **Speech-to-speech realtime APIs** (one model doing listening and speaking) are the other
   route to sub-second replies. Not tested; they send raw audio to a provider, which is a
   privacy decision for the threat model.
5. **Re-run on macOS (Apple Silicon) and a Windows laptop** before fixing the number; this is
   one Linux machine.
