#!/usr/bin/env python3
"""
Light Controller Daemon for Raspberry Pi 5
Controls head lights (GPIO 16) and tail lights (GPIO 20) via hardware PWM.
Listens on a Unix socket for commands from the web server.

GPIO Mapping (from project diagram):
  GPIO 16 (pin 36) -> MOSFET 1 SIG (1KΩ resistor) -> Head/Front lights
  GPIO 20 (pin 38) -> MOSFET 2 SIG (1KΩ resistor) -> Rear/Tail lights

PWM strategy (Pi 5 / libgpiod era):
  - GPIO 16 is NOT a hardware-PWM pin, so we use software PWM via gpiod +
    a tight Python thread. This is fine for light dimming (carrier ~500 Hz).
  - GPIO 20 is also a software-PWM pin for the same reason.
  - Hardware PWM (GPIO 12/13/18/19) could be used if the design changes.

Dependencies:
  pip3 install gpiod   (python bindings for libgpiod, Pi 5 native)

Command protocol (JSON over Unix socket, newline-delimited):
  {"channel": "head"|"tail", "intensity": 0.0-1.0, "mode": "steady"|"blink", "frequency": Hz}
  {"command": "status"}
  {"command": "all_off"}
"""

import json
import socket
import os
import subprocess
import threading
import time
import signal
import sys
import logging

SAFETY_SERVICE = "lights-safe-gpio.service"


def _systemctl(action: str):
    try:
        subprocess.run(["sudo", "systemctl", action, SAFETY_SERVICE],
                       check=True, capture_output=True)
        log.info(f"systemctl {action} {SAFETY_SERVICE}")
    except Exception as e:
        log.warning(f"systemctl {action} failed: {e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("light-controller")

# ---------------------------------------------------------------------------
# GPIO abstraction – real (gpiod) or stub
# ---------------------------------------------------------------------------

GPIOCHIP = "/dev/gpiochip0"  # Pi 5: 40-pin header
GPIO_HEAD = 16            # MOSFET 1 SIG - Front/Head lights
GPIO_TAIL = 20            # MOSFET 2 SIG - Rear/Tail lights
SOCKET_PATH = "/tmp/light_controller.sock"
PWM_CARRIER_HZ = 500      # Software-PWM carrier frequency for dimming

try:
    import gpiod
    from gpiod.line import Direction, Value

    def _open_line(gpio: int):
        """Request a GPIO line as output, initially LOW."""
        chip = gpiod.Chip(GPIOCHIP)
        return chip.request_lines(
            {gpio: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)},
            consumer="light-controller",
        )

    def _set_line(request, gpio: int, high: bool):
        request.set_value(gpio, Value.ACTIVE if high else Value.INACTIVE)

    def _release_line(request, gpio: int):
        _set_line(request, gpio, False)
        request.release()

    HAS_GPIO = True
    log.info(f"gpiod ready on {GPIOCHIP}")

except Exception as e:
    log.warning(f"gpiod not available ({e}), running in stub mode")
    HAS_GPIO = False

    def _open_line(gpio):   return object()
    def _set_line(req, gpio, high): pass
    def _release_line(req, gpio):   pass


CHANNELS = {
    "head": GPIO_HEAD,
    "tail": GPIO_TAIL,
}


# ---------------------------------------------------------------------------
# Per-channel controller
# ---------------------------------------------------------------------------

class LightChannel:
    """
    Manages one light channel.
    Steady mode: software PWM at PWM_CARRIER_HZ, duty = intensity.
    Blink mode:  PWM on for half-period then off for half-period at blink freq.
    """

    def __init__(self, name: str, gpio_pin: int):
        self.name = name
        self.pin = gpio_pin
        self.intensity = 0.0
        self.mode = "steady"
        self.frequency = 1.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._request = _open_line(gpio_pin)
        log.info(f"Channel '{name}' initialised on GPIO {gpio_pin} (LOW)")

    # ------------------------------------------------------------------
    # Internal PWM / blink loops
    # ------------------------------------------------------------------

    def _pwm_steady(self, intensity: float, stop: threading.Event):
        """Software PWM loop for steady dimming."""
        period = 1.0 / PWM_CARRIER_HZ
        on_time = period * intensity
        off_time = period * (1.0 - intensity)
        while not stop.is_set():
            if on_time > 0:
                _set_line(self._request, self.pin, True)
                stop.wait(on_time)
            if off_time > 0 and not stop.is_set():
                _set_line(self._request, self.pin, False)
                stop.wait(off_time)
        _set_line(self._request, self.pin, False)

    def _pwm_blink(self, intensity: float, frequency: float, stop: threading.Event):
        """Blink loop: PWM-dimmed ON for half blink period, then OFF."""
        blink_period = 1.0 / frequency
        half = blink_period / 2.0
        pwm_period = 1.0 / PWM_CARRIER_HZ
        on_time = pwm_period * intensity
        off_time = pwm_period * (1.0 - intensity)

        while not stop.is_set():
            # ON phase – run software PWM for `half` seconds
            phase_end = time.monotonic() + half
            while not stop.is_set() and time.monotonic() < phase_end:
                if on_time > 0:
                    _set_line(self._request, self.pin, True)
                    stop.wait(min(on_time, phase_end - time.monotonic()))
                if off_time > 0 and not stop.is_set():
                    _set_line(self._request, self.pin, False)
                    stop.wait(min(off_time, max(0, phase_end - time.monotonic())))
            if stop.is_set():
                break
            # OFF phase
            _set_line(self._request, self.pin, False)
            stop.wait(half)

        _set_line(self._request, self.pin, False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _stop_thread(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=3.0)
        self._stop_event.clear()
        self._thread = None

    def apply(self, intensity: float, mode: str, frequency: float = 1.0):
        with self._lock:
            self.intensity = max(0.0, min(1.0, intensity))
            self.mode = mode
            self.frequency = max(0.1, min(20.0, frequency))

            self._stop_thread()
            _set_line(self._request, self.pin, False)

            if self.intensity == 0.0:
                log.info(f"{self.name}: OFF")
                return

            if self.mode == "steady":
                if self.intensity >= 1.0:
                    # Full brightness – no PWM needed
                    _set_line(self._request, self.pin, True)
                    log.info(f"{self.name}: steady @ 100%")
                else:
                    stop = self._stop_event
                    self._thread = threading.Thread(
                        target=self._pwm_steady,
                        args=(self.intensity, stop),
                        daemon=True,
                    )
                    self._thread.start()
                    log.info(f"{self.name}: steady @ {self.intensity:.2f}")
            else:
                stop = self._stop_event
                self._thread = threading.Thread(
                    target=self._pwm_blink,
                    args=(self.intensity, self.frequency, stop),
                    daemon=True,
                )
                self._thread.start()
                log.info(f"{self.name}: blink @ {self.intensity:.2f}, {self.frequency:.2f}Hz")

    def off(self):
        self.apply(0.0, "steady")

    def status(self) -> dict:
        return {
            "channel": self.name,
            "intensity": self.intensity,
            "mode": self.mode,
            "frequency": self.frequency,
        }

    def cleanup(self):
        self._stop_thread()
        _set_line(self._request, self.pin, False)
        try:
            self._request.release()
        except Exception:
            pass


class LightController:
    def __init__(self):
        log.info("Stopping safety service to take over GPIO lines...")
        _systemctl("stop")
        time.sleep(0.5)  # give the kernel time to release the lines
        self.channels = {name: LightChannel(name, pin) for name, pin in CHANNELS.items()}
        self._running = False

    def handle_command(self, data: dict) -> dict:
        cmd = data.get("command")
        channel_name = data.get("channel")

        if cmd == "status":
            return {"status": "ok", "channels": [ch.status() for ch in self.channels.values()]}

        if cmd == "all_off":
            for ch in self.channels.values():
                ch.off()
            return {"status": "ok", "message": "all off"}

        if channel_name not in self.channels:
            return {"status": "error", "message": f"unknown channel '{channel_name}'"}

        channel = self.channels[channel_name]
        intensity = float(data.get("intensity", 0.0))
        mode = data.get("mode", "steady")
        frequency = float(data.get("frequency", 1.0))

        if mode not in ("steady", "blink"):
            return {"status": "error", "message": "mode must be 'steady' or 'blink'"}

        channel.apply(intensity, mode, frequency)
        return {"status": "ok", **channel.status()}

    def _handle_client(self, conn: socket.socket):
        try:
            with conn:
                buf = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            response = self.handle_command(data)
                        except json.JSONDecodeError as e:
                            response = {"status": "error", "message": f"invalid JSON: {e}"}
                        conn.sendall(json.dumps(response).encode() + b"\n")
        except Exception as e:
            log.error(f"Client error: {e}")

    def start_server(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o666)
        server.listen(5)
        server.settimeout(1.0)
        self._running = True
        log.info(f"Listening on {SOCKET_PATH}")

        while self._running:
            try:
                conn, _ = server.accept()
                t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except OSError:
                break

        server.close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

    def shutdown(self):
        self._running = False
        log.info("Shutting down - turning all lights off")
        for ch in self.channels.values():
            ch.off()
            ch.cleanup()
        log.info("Restarting safety service to re-secure GPIO lines...")
        _systemctl("start")


def main():
    controller = LightController()

    def on_signal(sig, frame):
        log.info(f"Received signal {sig}, shutting down")
        controller.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    controller.start_server()


if __name__ == "__main__":
    main()
