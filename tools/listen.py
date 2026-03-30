#!/usr/bin/env python3
"""
listen.py  —  Wake word + voice command listener
robot.beginner project · Raspberry Pi 5

Flow:
  1. Continuously stream audio from USB mic (at its native 44100Hz)
  2. Resample each chunk down to 16000Hz for openwakeword + Vosk
  3. openwakeword listens for "hey jarvis" (offline, no API key needed)
  4. On wake word: play a confirmation beep, record a short utterance
  5. Vosk transcribes the utterance offline
  6. Parsed command is dispatched to a callback (or printed for testing)

Usage (standalone test):
  cd ~/robot.beginner && source venv/bin/activate
  python3 tools/listen.py

Usage (imported into your own script):
  from tools.listen import CommandListener
  listener = CommandListener(on_command=my_handler)
  listener.start()
"""

import os
import time
import json
import logging
import pathlib
import threading

import pyaudio
import numpy as np
from vosk import Model, KaldiRecognizer
from openwakeword.model import Model as WakeModel
import openwakeword.utils

logging.basicConfig(level=logging.INFO, format='%(asctime)s [listen] %(message)s')
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — edit these if needed
# ---------------------------------------------------------------------------

VOSK_MODEL_PATH = os.path.expanduser('~/robot.beginner/models/vosk-model-small-en-us-0.15')

MIC_DEVICE_INDEX    = None    # None = auto-detect USB mic, or set to an int
MIC_SAMPLE_RATE     = 44100   # Hz — native rate of your USB PnP mic
TARGET_SAMPLE_RATE  = 16000   # Hz — required by Vosk and openwakeword
MIC_CHANNELS        = 1
MIC_FORMAT          = pyaudio.paInt16

# Chunk size at native rate — gives ~80ms frames after resampling (openwakeword sweet spot)
MIC_CHUNK           = int(MIC_SAMPLE_RATE * 0.08)   # 3528 samples @ 44100Hz ≈ 80ms

COMMAND_RECORD_SECS = 2.5     # seconds to record after wake word fires
WAKE_THRESHOLD      = 0.75    # 0.0–1.0, raised from 0.5 to reduce false positives

# Command keyword map  →  action string
# Add your own keywords and actions here as the robot grows
COMMAND_MAP = {
    'forward':  'move_forward',
    'ahead':    'move_forward',
    'back':     'move_backward',
    'backward': 'move_backward',
    'reverse':  'move_backward',
    'left':     'turn_left',
    'right':    'turn_right',
    'stop':     'stop',
    'halt':     'stop',
    'look':     'look',
    'see':      'look',
    'scan':     'look',
    'lights':   'toggle_lights',
    'light':    'toggle_lights',
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_usb_mic(pa: pyaudio.PyAudio) -> int:
    """Return device index of the first USB audio input found."""
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info['maxInputChannels'] > 0 and 'USB' in info['name']:
            log.info(f"Found USB mic: [{i}] {info['name']}")
            return i
    raise RuntimeError(
        'No USB microphone found.\n'
        'Run: arecord -l   to list devices\n'
        'Then set MIC_DEVICE_INDEX manually at the top of listen.py'
    )


def resample(chunk_int16: bytes) -> np.ndarray:
    """
    Convert a raw PCM bytes chunk from MIC_SAMPLE_RATE → TARGET_SAMPLE_RATE.
    Uses numpy linear interpolation — much faster than scipy on Pi 5,
    with negligible quality difference for speech recognition.
    Returns int16 numpy array ready for Vosk / openwakeword.
    """
    audio = np.frombuffer(chunk_int16, dtype=np.int16).astype(np.float32)
    n_out = int(len(audio) * TARGET_SAMPLE_RATE / MIC_SAMPLE_RATE)
    resampled = np.interp(
        np.linspace(0, len(audio) - 1, n_out),
        np.arange(len(audio)),
        audio,
    )
    return resampled.astype(np.int16)


def play_beep(pa: pyaudio.PyAudio, freq=880, duration=0.12, volume=0.4):
    """Play a short sine-wave beep so you know the wake word was heard."""
    sr = 44100
    samples = (
        np.sin(2 * np.pi * freq * np.linspace(0, duration, int(sr * duration)))
        * volume * 32767
    ).astype(np.int16)
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=sr, output=True)
    stream.write(samples.tobytes())
    stream.stop_stream()
    stream.close()


def parse_command(text: str) -> tuple[str | None, str]:
    """
    Match transcribed text against COMMAND_MAP.
    Returns (action, text) — action is None if no keyword matched.
    """
    text = text.lower().strip()
    for keyword, action in COMMAND_MAP.items():
        if keyword in text:
            return action, text
    return None, text


def resolve_wake_model() -> tuple[str, str]:
    """
    Find the hey_jarvis .onnx model bundled with openwakeword.
    Returns (full_path, inference_framework).
    Must pass inference_framework='onnx' explicitly — the default is
    'tflite' which crashes on Pi 5 / Python 3.13 where tflite is unavailable.
    """
    resources_dir = (
        pathlib.Path(openwakeword.utils.__file__).parent / 'resources' / 'models'
    )

    candidates = [
        ('hey_jarvis_v0.1.onnx',   'onnx'),
        ('hey_jarvis.onnx',        'onnx'),
        ('hey_jarvis_v0.1.tflite', 'tflite'),
        ('hey_jarvis.tflite',      'tflite'),
    ]

    for fname, framework in candidates:
        path = resources_dir / fname
        if path.exists():
            log.info(f'Wake word model found: {path.name} (framework={framework})')
            return str(path), framework

    # Not bundled — try downloading once
    log.info('hey_jarvis model not found locally, attempting download...')
    try:
        openwakeword.utils.download_models(model_names=['hey_jarvis_v0.1'])
    except Exception as e:
        raise FileNotFoundError(
            f'Could not download hey_jarvis model: {e}\n'
            f'Expected files in: {resources_dir}'
        )

    for fname, framework in candidates:
        path = resources_dir / fname
        if path.exists():
            return str(path), framework

    raise FileNotFoundError(
        f'hey_jarvis model still not found after download.\n'
        f'Files in resources dir: {list(resources_dir.iterdir())}'
    )


# ---------------------------------------------------------------------------
# Main listener class
# ---------------------------------------------------------------------------

class CommandListener:
    """
    Listens for "Hey Jarvis" then transcribes and dispatches voice commands.

    Parameters
    ----------
    on_command : callable(action: str, text: str)
        Called each time a recognised command is heard.
        action — matched key from COMMAND_MAP  e.g. 'move_forward'
        text   — raw transcription              e.g. 'go forward please'
    on_wake : callable() | None
        Optional — called the instant the wake word fires, before
        recording starts. Useful to pause motors, LEDs, etc.
    """

    def __init__(self, on_command=None, on_wake=None):
        self._on_command = on_command or self._default_handler
        self._on_wake    = on_wake
        self._running    = False
        self._thread     = None

        # Load Vosk speech-to-text model
        log.info('Loading Vosk model...')
        if not os.path.exists(VOSK_MODEL_PATH):
            raise FileNotFoundError(
                f'Vosk model not found at: {VOSK_MODEL_PATH}\n'
                'Re-run setup.sh to download it automatically.'
            )
        self._vosk = Model(VOSK_MODEL_PATH)
        log.info('Vosk ready')

        # Load openwakeword hey_jarvis model
        log.info('Loading openwakeword model...')
        model_path, framework = resolve_wake_model()
        self._wake_model = WakeModel(
            wakeword_models=[model_path],
            inference_framework=framework,   # critical — must match file type
        )
        log.info(f'Wake word ready (threshold={WAKE_THRESHOLD})')
        log.info(f'Mic: {MIC_SAMPLE_RATE}Hz → resampling to {TARGET_SAMPLE_RATE}Hz')

    # ------------------------------------------------------------------
    def start(self):
        """Start the listener in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info('Listening — say "Hey Jarvis" to activate')

    def stop(self):
        """Stop the background thread cleanly."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        log.info('Listener stopped')

    # ------------------------------------------------------------------
    def _flush_stream(self, stream):
        """Stop and restart the stream in-place to clear the buffer without changing device."""
        try:
            stream.stop_stream()
            stream.start_stream()
        except Exception as e:
            log.warning(f'Stream flush warning: {e}')

    def _run(self):
        pa = pyaudio.PyAudio()
        stream = None
        try:
            dev_idx = (
                MIC_DEVICE_INDEX if MIC_DEVICE_INDEX is not None
                else find_usb_mic(pa)
            )
            stream = pa.open(
                format=MIC_FORMAT,
                channels=MIC_CHANNELS,
                rate=MIC_SAMPLE_RATE,
                input=True,
                input_device_index=dev_idx,
                frames_per_buffer=MIC_CHUNK,
            )
            log.info(f'Audio stream open — device {dev_idx} @ {MIC_SAMPLE_RATE}Hz')

            _last_heartbeat = time.time()
            while self._running:
                # ── Wake word detection ──────────────────────────────────
                raw = stream.read(MIC_CHUNK, exception_on_overflow=False)
                audio_16k = resample(raw)
                prediction = self._wake_model.predict(audio_16k)

                score = 0.0
                for k, v in prediction.items():
                    if 'hey_jarvis' in k.lower():
                        score = v
                        break

                if score < WAKE_THRESHOLD:
                    now = time.time()
                    if now - _last_heartbeat >= 10:
                        log.info(f'Still listening... say "Hey Jarvis" (last score={score:.3f})')
                        _last_heartbeat = now
                    continue

                log.info(f'Wake word detected! (score={score:.2f})')
                play_beep(pa)

                if self._on_wake:
                    self._on_wake()

                # ── Command recording ────────────────────────────────────
                # Flush buffer before recording so we don't transcribe stale audio.
                self._flush_stream(stream)
                log.info(f'Recording for {COMMAND_RECORD_SECS}s...')

                rec = KaldiRecognizer(self._vosk, TARGET_SAMPLE_RATE)
                rec.SetWords(False)

                # Record until time limit or silence after speech is detected.
                # RMS silence threshold — below this level counts as quiet.
                SILENCE_RMS       = 150    # ~-46 dBFS on int16 scale
                SILENCE_CHUNKS    = 8      # consecutive quiet chunks = end of speech (~640ms)
                n_chunks          = int(MIC_SAMPLE_RATE / MIC_CHUNK * COMMAND_RECORD_SECS)
                silence_count     = 0
                got_speech        = False
                for _ in range(n_chunks):
                    if not self._running:
                        break
                    raw = stream.read(MIC_CHUNK, exception_on_overflow=False)
                    audio_16k = resample(raw)
                    rec.AcceptWaveform(audio_16k.tobytes())
                    rms = int(np.sqrt(np.mean(audio_16k.astype(np.float32) ** 2)))
                    if rms > SILENCE_RMS:
                        got_speech    = True
                        silence_count = 0
                    elif got_speech:
                        silence_count += 1
                        if silence_count >= SILENCE_CHUNKS:
                            log.info('Silence detected — stopping recording early')
                            break

                text = json.loads(rec.FinalResult()).get('text', '').strip()
                log.info(f'Heard: "{text}"')

                if not text:
                    log.info('Nothing transcribed — back to listening')
                else:
                    action, text = parse_command(text)
                    if action:
                        log.info(f'Command: {action}')
                        self._on_command(action, text)
                    else:
                        log.info(f'No matching command in: "{text}"')

                # Cooldown: flush and drain for 1.5s to reset openwakeword's
                # sliding window and suppress false re-triggers.
                self._flush_stream(stream)
                log.info('Cooldown — draining mic for 1.5s...')
                cooldown_end = time.time() + 1.5
                while time.time() < cooldown_end:
                    raw = stream.read(MIC_CHUNK, exception_on_overflow=False)
                    self._wake_model.predict(resample(raw))  # discard
                _last_heartbeat = time.time()
                log.info('Cooldown done — back to listening')

        except Exception as e:
            log.error(f'Listener error: {e}')
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()

    # ------------------------------------------------------------------
    @staticmethod
    def _default_handler(action: str, text: str):
        print(f'\n  ACTION : {action}')
        print(f'  TEXT   : {text}\n')


# ---------------------------------------------------------------------------
# Standalone test — python3 tools/listen.py
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print('\nrobot.beginner — voice listener test')
    print('Say "Hey Jarvis" then a command (stop / forward / look / etc.)')
    print('Ctrl+C to quit\n')

    listener = CommandListener()
    listener.start()

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print('\nStopping...')
        listener.stop()
