"""Generate synthetic test utterances (piper -> 16 kHz mono WAV). No mic, no speakers."""
import numpy as np
import soundfile as sf
from piper import PiperVoice

from common import AUDIO, MODELS, UTTERANCES

AUDIO.mkdir(exist_ok=True)
voice = PiperVoice.load(str(MODELS / "en_US-lessac-medium.onnx"))
for name, text in UTTERANCES.items():
    chunks = [c.audio_float_array for c in voice.synthesize(text)]
    sr = voice.config.sample_rate
    audio = np.concatenate(chunks).astype(np.float32)
    # resample to 16 kHz with linear interpolation (adequate for speech at 22.05k -> 16k)
    n16 = int(len(audio) * 16000 / sr)
    a16 = np.interp(np.linspace(0, len(audio) - 1, n16), np.arange(len(audio)), audio).astype(np.float32)
    # 0.5 s of silence lead-in/out so VAD has something realistic to trim
    pad = np.zeros(8000, dtype=np.float32)
    a16 = np.concatenate([pad, a16, pad])
    sf.write(AUDIO / f"{name}.wav", a16, 16000, subtype="PCM_16")
    print(f"{name}: {len(a16)/16000:.2f}s (speech {len(audio)/sr:.2f}s) -> {AUDIO/f'{name}.wav'}")
