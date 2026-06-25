#!/bin/bash
# Install kokoro-onnx (lighter than torch, CPU-optimized, no Python<3.13 cap) + the model files,
# into the Pi's TTS venv. Run detached; progress in ~/voice/pi/onnx-install.log.
set -u
LOG=~/voice/pi/onnx-install.log
VENV=~/voice/pi/.venv-kokoro
echo "=== ONNX install $(date) ===" > "$LOG"
"$VENV"/bin/pip install kokoro-onnx soundfile httpx realtime >> "$LOG" 2>&1
echo "--- pip rc=$?, fetching model files ---" >> "$LOG"
cd ~/voice/pi || exit 1
wget -q https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx -O kokoro-v1.0.onnx >> "$LOG" 2>&1
echo "onnx-model rc=$?" >> "$LOG"
wget -q https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin -O voices-v1.0.bin >> "$LOG" 2>&1
echo "voices rc=$?" >> "$LOG"
ls -la kokoro-v1.0.onnx voices-v1.0.bin >> "$LOG" 2>&1
echo "ONNX_DONE $(date)" >> "$LOG"
