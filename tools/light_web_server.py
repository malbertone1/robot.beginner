#!/usr/bin/env python3
"""
Light Testing Web Server
Exposes a REST API and web UI to control head and tail lights on the Pi5.
Communicates with light_controller.py via Unix socket.

Usage:
    python3 light_web_server.py [--port 5000] [--host 0.0.0.0]

Endpoints:
    GET  /              -> Web UI
    GET  /api/status    -> Get current state of all channels
    POST /api/control   -> Set light parameters
    POST /api/all_off   -> Turn all lights off
"""

import json
import socket
import argparse
import logging
from flask import Flask, request, jsonify, render_template_string

SOCKET_PATH = "/tmp/light_controller.sock"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("light-webserver")

app = Flask(__name__)


def send_to_controller(payload: dict) -> dict:
    """Send a JSON command to the light controller daemon via Unix socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if b"\n" in response:
                break
        sock.close()
        return json.loads(response.strip())
    except FileNotFoundError:
        return {"status": "error", "message": "Controller daemon not running (socket not found)"}
    except ConnectionRefusedError:
        return {"status": "error", "message": "Controller daemon refused connection"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(send_to_controller({"command": "status"}))


@app.route("/api/control", methods=["POST"])
def api_control():
    data = request.get_json(force=True)
    channel = data.get("channel")
    if channel not in ("head", "tail"):
        return jsonify({"status": "error", "message": "channel must be 'head' or 'tail'"}), 400
    try:
        intensity = float(data.get("intensity", 0.0))
        frequency = float(data.get("frequency", 1.0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "intensity and frequency must be numbers"}), 400
    mode = data.get("mode", "steady")
    result = send_to_controller({
        "channel": channel,
        "intensity": intensity,
        "mode": mode,
        "frequency": frequency,
    })
    return jsonify(result)


@app.route("/api/all_off", methods=["POST"])
def api_all_off():
    return jsonify(send_to_controller({"command": "all_off"}))


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Light Controller – Pi5 Test Tool</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --border: #2e3148;
    --accent: #4f8ef7;
    --accent2: #f7b84f;
    --text: #e2e4ef;
    --muted: #6b7280;
    --success: #34d399;
    --danger: #f87171;
    --radius: 12px;
  }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
         min-height: 100vh; padding: 2rem 1rem; }
  h1 { text-align: center; font-size: 1.6rem; margin-bottom: 0.25rem; color: var(--accent); }
  .subtitle { text-align: center; color: var(--muted); font-size: 0.85rem; margin-bottom: 2rem; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; max-width: 860px; margin: 0 auto 1.5rem; }
  @media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 1.5rem; }
  .card-title { font-size: 1.1rem; font-weight: 700; margin-bottom: 1.25rem; display: flex; align-items: center; gap: 0.5rem; }
  .dot { width: 12px; height: 12px; border-radius: 50%; background: var(--muted); transition: background 0.3s; flex-shrink: 0; }
  .dot.on-steady { background: var(--success); box-shadow: 0 0 8px var(--success); }
  .dot.on-blink  { background: var(--accent2); box-shadow: 0 0 8px var(--accent2); animation: blink-indicator 1s step-start infinite; }
  @keyframes blink-indicator { 50% { opacity: 0; } }

  label { display: block; font-size: 0.8rem; color: var(--muted); margin-bottom: 0.35rem; }
  .field { margin-bottom: 1rem; }
  input[type=number], input[type=range] { width: 100%; }
  input[type=number] {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); padding: 0.45rem 0.65rem; font-size: 0.95rem;
  }
  input[type=range] { accent-color: var(--accent); cursor: pointer; }

  .mode-row { display: flex; gap: 0.75rem; margin-bottom: 1rem; }
  .mode-btn {
    flex: 1; padding: 0.45rem; border: 1px solid var(--border); border-radius: 6px;
    background: var(--bg); color: var(--muted); cursor: pointer; font-size: 0.85rem;
    transition: all 0.15s;
  }
  .mode-btn.active { border-color: var(--accent); color: var(--accent); background: rgba(79,142,247,0.1); }

  .blink-opts { transition: opacity 0.2s; }
  .blink-opts.hidden { opacity: 0.3; pointer-events: none; }

  .apply-btn {
    width: 100%; padding: 0.65rem; border: none; border-radius: 8px;
    background: var(--accent); color: #fff; font-size: 0.95rem; font-weight: 600;
    cursor: pointer; transition: opacity 0.15s; margin-top: 0.25rem;
  }
  .apply-btn:hover { opacity: 0.85; }
  .apply-btn:active { opacity: 0.7; }

  .footer-row { max-width: 860px; margin: 0 auto; display: flex; gap: 1rem; }
  .all-off-btn {
    flex: 1; padding: 0.75rem; border: 1px solid var(--danger); border-radius: 8px;
    background: transparent; color: var(--danger); font-size: 0.95rem; font-weight: 600;
    cursor: pointer; transition: all 0.15s;
  }
  .all-off-btn:hover { background: rgba(248,113,113,0.1); }

  .status-bar {
    max-width: 860px; margin: 1.25rem auto 0;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 0.75rem 1.25rem;
    font-size: 0.82rem; color: var(--muted); display: flex; align-items: center; gap: 0.5rem;
  }
  .status-bar span { color: var(--text); }
  #status-msg.ok   { color: var(--success); }
  #status-msg.err  { color: var(--danger); }

  .gpio-info { font-size: 0.75rem; color: var(--muted); margin-top: 0.75rem; text-align: right; }
</style>
</head>
<body>

<h1>🔦 Light Controller</h1>
<p class="subtitle">Pi5 Hardware Test Tool &nbsp;|&nbsp; GPIO 16 = Head &nbsp;|&nbsp; GPIO 20 = Tail</p>

<div class="grid">
  <!-- HEAD LIGHTS -->
  <div class="card" id="card-head">
    <div class="card-title">
      <span class="dot" id="dot-head"></span>
      Head Lights &nbsp;<small style="color:var(--muted);font-weight:400">(GPIO 16)</small>
    </div>

    <div class="field">
      <label>Intensity (0 – 1)</label>
      <input type="range" id="head-slider" min="0" max="1" step="0.01" value="0"
             oninput="document.getElementById('head-int').value=parseFloat(this.value).toFixed(2)">
      <input type="number" id="head-int" min="0" max="1" step="0.01" value="0.00"
             oninput="document.getElementById('head-slider').value=this.value">
    </div>

    <div class="field">
      <label>Mode</label>
      <div class="mode-row">
        <button class="mode-btn active" id="head-steady-btn" onclick="setMode('head','steady')">Steady</button>
        <button class="mode-btn"        id="head-blink-btn"  onclick="setMode('head','blink')">Blink</button>
      </div>
    </div>

    <div class="field blink-opts hidden" id="head-blink-opts">
      <label>Blink Frequency (Hz)</label>
      <input type="number" id="head-freq" min="0.1" max="20" step="0.1" value="1.0">
    </div>

    <button class="apply-btn" onclick="applyChannel('head')">Apply Head Lights</button>
    <p class="gpio-info">MOSFET 1 · 1 KΩ resistor</p>
  </div>

  <!-- TAIL LIGHTS -->
  <div class="card" id="card-tail">
    <div class="card-title">
      <span class="dot" id="dot-tail"></span>
      Tail Lights &nbsp;<small style="color:var(--muted);font-weight:400">(GPIO 20)</small>
    </div>

    <div class="field">
      <label>Intensity (0 – 1)</label>
      <input type="range" id="tail-slider" min="0" max="1" step="0.01" value="0"
             oninput="document.getElementById('tail-int').value=parseFloat(this.value).toFixed(2)">
      <input type="number" id="tail-int" min="0" max="1" step="0.01" value="0.00"
             oninput="document.getElementById('tail-slider').value=this.value">
    </div>

    <div class="field">
      <label>Mode</label>
      <div class="mode-row">
        <button class="mode-btn active" id="tail-steady-btn" onclick="setMode('tail','steady')">Steady</button>
        <button class="mode-btn"        id="tail-blink-btn"  onclick="setMode('tail','blink')">Blink</button>
      </div>
    </div>

    <div class="field blink-opts hidden" id="tail-blink-opts">
      <label>Blink Frequency (Hz)</label>
      <input type="number" id="tail-freq" min="0.1" max="20" step="0.1" value="1.0">
    </div>

    <button class="apply-btn" onclick="applyChannel('tail')">Apply Tail Lights</button>
    <p class="gpio-info">MOSFET 2 · 1 KΩ resistor</p>
  </div>
</div>

<div class="footer-row">
  <button class="all-off-btn" onclick="allOff()">⏻ All Off</button>
</div>

<div class="status-bar">
  Last action: <span id="status-msg">–</span>
</div>

<script>
  const modes = { head: "steady", tail: "steady" };

  function setMode(ch, mode) {
    modes[ch] = mode;
    document.getElementById(`${ch}-steady-btn`).classList.toggle("active", mode === "steady");
    document.getElementById(`${ch}-blink-btn`).classList.toggle("active",  mode === "blink");
    document.getElementById(`${ch}-blink-opts`).classList.toggle("hidden", mode !== "blink");
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  async function applyChannel(ch) {
    const intensity = clamp(parseFloat(document.getElementById(`${ch}-int`).value) || 0, 0, 1);
    const frequency = clamp(parseFloat(document.getElementById(`${ch}-freq`).value) || 1, 0.1, 20);
    const mode = modes[ch];

    // Sync slider
    document.getElementById(`${ch}-slider`).value = intensity;
    document.getElementById(`${ch}-int`).value = intensity.toFixed(2);

    const res = await post("/api/control", { channel: ch, intensity, mode, frequency });
    showStatus(res);
    updateDot(ch, intensity, mode, frequency);
  }

  async function allOff() {
    const res = await post("/api/all_off", {});
    showStatus(res);
    ["head","tail"].forEach(ch => updateDot(ch, 0, "steady", 1));
  }

  async function post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
      return await r.json();
    } catch(e) { return { status: "error", message: String(e) }; }
  }

  function showStatus(res) {
    const el = document.getElementById("status-msg");
    if (res.status === "ok") {
      const ch = res.channel ? `${res.channel}: ${res.mode} @ ${(res.intensity*100).toFixed(0)}%` + (res.mode==="blink" ? ` ${res.frequency}Hz` : "") : "all off";
      el.textContent = ch;
      el.className = "ok";
    } else {
      el.textContent = res.message || "error";
      el.className = "err";
    }
  }

  function updateDot(ch, intensity, mode, freq) {
    const dot = document.getElementById(`dot-${ch}`);
    dot.className = "dot";
    if (intensity > 0) dot.classList.add(mode === "blink" ? "on-blink" : "on-steady");
    // Sync blink indicator speed to actual freq
    if (mode === "blink" && freq) {
      dot.style.animationDuration = `${(1/freq).toFixed(2)}s`;
    }
  }

  // Poll status on load
  (async () => {
    const res = await fetch("/api/status");
    if (!res.ok) return;
    const data = await res.json();
    if (data.channels) {
      data.channels.forEach(ch => {
        const name = ch.channel;
        document.getElementById(`${name}-int`).value = ch.intensity.toFixed(2);
        document.getElementById(`${name}-slider`).value = ch.intensity;
        document.getElementById(`${name}-freq`).value = ch.frequency.toFixed(1);
        setMode(name, ch.mode);
        updateDot(name, ch.intensity, ch.mode, ch.frequency);
      });
    }
  })();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Light Testing Web Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Bind port (default: 5000)")
    args = parser.parse_args()
    log.info(f"Starting web server on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
