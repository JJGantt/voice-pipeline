import os, time, soundfile as sf
from kokoro_onnx import Kokoro
MODEL = os.environ.get("KOKORO_MODEL", "/home/jaredgantt/voice/pi/kokoro-v1.0.onnx")
t0 = time.monotonic()
kok = Kokoro(MODEL, "/home/jaredgantt/voice/pi/voices-v1.0.bin")
print(f"model {os.path.basename(MODEL)} load: {time.monotonic()-t0:.1f}s")
kok.create("ready", voice="af_heart", speed=1.0, lang="en-us")  # warm
samples = sr = None
for txt in ["Logged your weight at 172.4 pounds.",
            "Added the chicken and rice to today's nutrition.",
            "You've eaten about 1,800 calories so far today."]:
    t = time.monotonic()
    samples, sr = kok.create(txt, voice="af_heart", speed=1.0, lang="en-us")
    audio_s = len(samples) / sr
    print(f"synth {time.monotonic()-t:.2f}s  (audio {audio_s:.1f}s, RTF {(time.monotonic()-t)/audio_s:.2f})  {txt!r}")
sf.write("/tmp/tts_test.wav", samples, sr)
print("wrote /tmp/tts_test.wav")
