#!/usr/bin/env bash
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
MODELS_DIR="$PROJECT_DIR/models"
VOSK_MODEL_NAME="vosk-model-small-en-us-0.15"
VOSK_MODEL_URL="https://alphacephei.com/vosk/models/${VOSK_MODEL_NAME}.zip"

echo ""
echo "========================================="
echo "  robot.beginner — setup"
echo "========================================="

echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq portaudio19-dev libportaudio2 libasound-dev wget unzip python3-pip python3-venv

echo "[2/5] Creating Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "  Created: $VENV_DIR"
else
    echo "  Already exists: $VENV_DIR (skipping)"
fi
source "$VENV_DIR/bin/activate"

echo "[3/5] Installing Python packages..."
pip install --quiet --upgrade pip
pip install --quiet pyaudio numpy vosk
pip install --quiet openwakeword --no-deps
pip install --quiet onnxruntime scipy

echo "[4/5] Setting up Vosk model..."
mkdir -p "$MODELS_DIR"
if [ -d "$MODELS_DIR/$VOSK_MODEL_NAME" ]; then
    echo "  Already downloaded: $VOSK_MODEL_NAME (skipping)"
else
    echo "  Downloading $VOSK_MODEL_NAME (~40MB)..."
    wget -q --show-progress -O "$MODELS_DIR/${VOSK_MODEL_NAME}.zip" "$VOSK_MODEL_URL"
    unzip -q "$MODELS_DIR/${VOSK_MODEL_NAME}.zip" -d "$MODELS_DIR/"
    rm "$MODELS_DIR/${VOSK_MODEL_NAME}.zip"
    echo "  Done."
fi

echo "[5/5] Verifying hey_jarvis wake word model..."
python3 - <<'PYEOF'
import pathlib, sys
import openwakeword.utils as owu
resources_dir = pathlib.Path(owu.__file__).parent / 'resources' / 'models'
found = list(resources_dir.glob('hey_jarvis*.onnx'))
if found:
    print(f"  Model ready: {found[0].name}")
else:
    print("  Downloading hey_jarvis model...")
    owu.download_models(model_names=['hey_jarvis_v0.1'])
    print("  Done.")
PYEOF

echo ""
echo "========================================="
echo "  Setup complete!"
echo "  source venv/bin/activate"
echo "  python3 tools/listen.py"
echo "========================================="
