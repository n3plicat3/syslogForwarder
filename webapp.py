#!/usr/bin/env python3
import json
import os
import threading
from collections import deque
import random
import time
from pathlib import Path
from queue import Queue, Empty
from typing import Dict, List

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from syslog_forwarder import (
    FACILITY_MAP,
    SEVERITY_MAP,
    TailManager,
    build_sender,
    load_config,
)


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret")


# ----------------------------
# Global state
# ----------------------------

STATE_LOCK = threading.Lock()
MANAGER_THREAD: threading.Thread | None = None
TAIL_MANAGER: TailManager | None = None
CURRENT_CONFIG: Dict = {}

# Event broadcasting (simple in-process pub/sub)
SUBSCRIBERS: List[Queue] = []
EVENT_HISTORY = deque(maxlen=500)

# JSON simulation/forwarder state
JSON_TEMPLATES: Dict[str, dict] = {}
JSON_SAMPLES: Dict[str, list] = {}
JSON_FORWARDER_THREAD: threading.Thread | None = None
JSON_FORWARDER_STOP = threading.Event()
JSON_FORWARDER_CFG: Dict = {}


def broadcast_event(evt: Dict):
    EVENT_HISTORY.append(evt)
    dead: List[Queue] = []
    for q in list(SUBSCRIBERS):
        try:
            q.put_nowait(evt)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            SUBSCRIBERS.remove(q)
        except ValueError:
            pass


def tail_on_event(evt: Dict):
    # Adapter used by TailManager to publish events
    broadcast_event(evt)


def is_running() -> bool:
    with STATE_LOCK:
        return MANAGER_THREAD is not None and MANAGER_THREAD.is_alive()


def start_forwarder(cfg: Dict):
    global MANAGER_THREAD, TAIL_MANAGER, CURRENT_CONFIG
    with STATE_LOCK:
        if is_running():
            return
        CURRENT_CONFIG = cfg
        sender = build_sender(cfg)

        syslog_cfg = cfg.get("syslog", {})
        facility_name = (syslog_cfg.get("facility") or "user").lower()
        severity_name = (syslog_cfg.get("severity") or "info").lower()
        facility = FACILITY_MAP[facility_name]
        severity = SEVERITY_MAP[severity_name]
        app_name = syslog_cfg.get("app_name") or "forwarder"
        host_name = syslog_cfg.get("host_name") or None

        files_cfg = cfg.get("files", {})
        pattern = files_cfg.get("pattern", "*.log")
        read_from_beginning = bool(files_cfg.get("read_from_beginning", False))
        rescan_interval = float(files_cfg.get("rescan_interval_sec", 5))

        directory = Path.cwd()
        manager = TailManager(
            sender=sender,
            pattern=pattern,
            facility=facility,
            severity=severity,
            app_name=app_name,
            host_name=host_name,
            read_from_beginning=read_from_beginning,
            rescan_interval=rescan_interval,
            directory=directory,
            on_event=tail_on_event,
        )

        TAIL_MANAGER = manager
        MANAGER_THREAD = threading.Thread(target=manager.start, daemon=True)
        MANAGER_THREAD.start()
        broadcast_event({"type": "status", "status": "started"})


def stop_forwarder():
    global MANAGER_THREAD, TAIL_MANAGER
    with STATE_LOCK:
        if TAIL_MANAGER is not None:
            try:
                TAIL_MANAGER.stop()
            except Exception:
                pass
        if MANAGER_THREAD is not None:
            MANAGER_THREAD.join(timeout=2.0)
        MANAGER_THREAD = None
        TAIL_MANAGER = None
        broadcast_event({"type": "status", "status": "stopped"})


def load_current_config() -> Dict:
    cfg_path = "config.json" if os.path.exists("config.json") else None
    return load_config(cfg_path)


# ----------------------------
# JSON helpers
# ----------------------------

def _rand_like(value):
    # Generate a random value similar to the input value
    if isinstance(value, bool):
        return bool(random.getrandbits(1))
    if isinstance(value, int):
        base = value
        jitter = max(1, abs(base) // 10)
        return base + random.randint(-jitter, jitter)
    if isinstance(value, float):
        base = value
        jitter = abs(base) * 0.1 if base != 0 else 1.0
        return round(base + random.uniform(-jitter, jitter), 6)
    if isinstance(value, str):
        # Try timestamp-ish replacement if it looks like one
        if len(value) >= 10 and any(ch.isdigit() for ch in value):
            # Recent unix timestamp variant
            now = int(time.time())
            return str(now - random.randint(0, 3600))
        # Otherwise random suffix/prefix
        prefix = value[: max(0, min(len(value), 6))]
        return f"{prefix}{random.randint(1000, 9999)}"
    if isinstance(value, dict):
        return {k: _rand_like(v) for k, v in value.items()}
    if isinstance(value, list):
        # Vary length slightly
        n = len(value)
        n2 = max(0, min(n + random.randint(-1, 2), n + 2))
        base = value or ["item"]
        return [_rand_like(random.choice(base)) for _ in range(n2)]
    return value


def generate_samples_from_template(template: dict, count: int = 100) -> list:
    samples = []
    for _ in range(count):
        def mutate(obj):
            if isinstance(obj, dict):
                return {k: mutate(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [mutate(v) for v in obj]
            return _rand_like(obj)

        samples.append(mutate(template))
    return samples


def ensure_json_loaded():
    # Load any .json files in CWD as templates if not already loaded
    for p in Path.cwd().glob("*.json"):
        name = p.stem
        if name not in JSON_TEMPLATES:
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                # If array, take first object as template; else object
                if isinstance(data, list) and data:
                    data = data[0]
                if isinstance(data, dict):
                    JSON_TEMPLATES[name] = data
                    JSON_SAMPLES[name] = generate_samples_from_template(data, 100)
            except Exception:
                continue


def start_json_forwarder(name: str, mps: float):
    global JSON_FORWARDER_THREAD
    ensure_json_loaded()
    if name not in JSON_SAMPLES:
        raise ValueError("Unknown json template name")
    if JSON_FORWARDER_THREAD is not None and JSON_FORWARDER_THREAD.is_alive():
        return

    JSON_FORWARDER_STOP.clear()

    cfg = CURRENT_CONFIG or load_current_config()
    JSON_FORWARDER_CFG.update({
        "name": name,
        "mps": mps,
    })

    def run():
        try:
            sender = build_sender(cfg)
            syslog_cfg = cfg.get("syslog", {})
            facility = FACILITY_MAP.get((syslog_cfg.get("facility") or "user"), 1)
            severity = SEVERITY_MAP.get((syslog_cfg.get("severity") or "info"), 6)
            app_name = (syslog_cfg.get("app_name") or "json-forwarder")
            host_name = syslog_cfg.get("host_name") or None
            procid = str(os.getpid())
            idx = 0
            period = 1.0 / max(0.1, float(mps))
            broadcast_event({"type": "json_forwarder", "status": "started", "name": name, "mps": mps})
            samples = JSON_SAMPLES[name]
            while not JSON_FORWARDER_STOP.is_set():
                payload = samples[idx % len(samples)]
                msg = json.dumps(payload, separators=(",", ":"))
                pri = facility * 8 + severity
                timestamp = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).astimezone().isoformat()
                hostname = host_name or os.uname().nodename
                version = 1
                syslog_msg = f"<{pri}>{version} {timestamp} {hostname} {app_name} {procid} - - {msg}"
                try:
                    sender.send(syslog_msg)
                    broadcast_event({"type": "json_line", "timestamp": timestamp, "app": app_name, "message": msg})
                except Exception:
                    pass
                idx += 1
                time.sleep(period)
        finally:
            try:
                sender.close()
            except Exception:
                pass
            broadcast_event({"type": "json_forwarder", "status": "stopped", "name": name})

    JSON_FORWARDER_THREAD = threading.Thread(target=run, daemon=True)
    JSON_FORWARDER_THREAD.start()


def stop_json_forwarder():
    JSON_FORWARDER_STOP.set()
    if JSON_FORWARDER_THREAD is not None:
        JSON_FORWARDER_THREAD.join(timeout=2.0)


@app.route("/")
def index():
    cfg = CURRENT_CONFIG or load_current_config()
    files_cfg = cfg.get("files", {})
    pattern = files_cfg.get("pattern", "*.log")
    directory = Path.cwd()
    patterns = [p.strip() for p in str(pattern).split(',') if p.strip()]
    if not patterns:
        patterns = ['*.log']
    seen = set()
    for pat in patterns:
        for p in directory.glob(pat):
            seen.add(str(p.name))
    files = sorted(list(seen))
    dest = cfg.get("destination", {})
    ensure_json_loaded()
    json_sets = sorted(list(JSON_TEMPLATES.keys()))
    return render_template(
        "index.html",
        running=is_running(),
        cfg=cfg,
        files=files,
        dest=dest,
        json_sets=json_sets,
    )


@app.route("/start", methods=["POST"])
def start():
    cfg = CURRENT_CONFIG or load_current_config()
    start_forwarder(cfg)
    flash("Forwarder started.")
    return redirect(url_for("index"))


@app.route("/stop", methods=["POST"])
def stop():
    stop_forwarder()
    flash("Forwarder stopped.")
    return redirect(url_for("index"))


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or f.filename == "":
        flash("No file selected.")
        return redirect(url_for("index"))
    filename = secure_filename(f.filename)
    target = Path.cwd() / filename
    f.save(str(target))
    try:
        sz = target.stat().st_size
    except Exception:
        sz = None
    # If JSON file, load as template and generate default samples
    if filename.lower().endswith(".json"):
        try:
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                name = Path(filename).stem
                JSON_TEMPLATES[name] = data
                JSON_SAMPLES[name] = generate_samples_from_template(data, 100)
                flash(f"Uploaded and prepared JSON template '{name}' with 100 samples.")
            else:
                flash("JSON must be an object or array of objects.")
        except Exception as e:
            flash(f"Failed to parse JSON: {e}")
    else:
        flash(f"Uploaded {filename}.")
    # Emit an event so the UI can show upload confirmation in Live Log
    try:
        broadcast_event({"type": "upload", "filename": filename, "size": sz})
    except Exception:
        pass
    return redirect(url_for("index"))


@app.route("/config", methods=["GET", "POST"])
def config_view():
    if request.method == "POST":
        # Build config from form values
        cfg = load_current_config()
        dest = cfg.setdefault("destination", {})
        files = cfg.setdefault("files", {})
        syslog = cfg.setdefault("syslog", {})
        tls = cfg.setdefault("tls", {})

        dest["host"] = request.form.get("host", dest.get("host", "127.0.0.1"))
        dest["port"] = int(request.form.get("port", dest.get("port", 514)))
        dest["protocol"] = request.form.get("protocol", dest.get("protocol", "udp"))

        files["pattern"] = request.form.get("pattern", files.get("pattern", "*.log"))
        files["read_from_beginning"] = bool(request.form.get("from_beginning"))
        files["rescan_interval_sec"] = float(
            request.form.get("rescan_interval_sec", files.get("rescan_interval_sec", 5))
        )

        syslog["facility"] = request.form.get("facility", syslog.get("facility", "user"))
        syslog["severity"] = request.form.get("severity", syslog.get("severity", "info"))
        syslog["app_name"] = request.form.get("app_name", syslog.get("app_name", "forwarder"))
        syslog["host_name"] = request.form.get("host_name") or None

        tls["ca_file"] = request.form.get("ca_file") or None
        tls["cert_file"] = request.form.get("cert_file") or None
        tls["key_file"] = request.form.get("key_file") or None
        tls["verify_mode"] = request.form.get("verify_mode", tls.get("verify_mode", "required"))

        # Persist to config.json
        with open("config.json", "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

        # If running, restart with new config
        if is_running():
            stop_forwarder()
            start_forwarder(cfg)
        else:
            # Keep the in-memory one fresh
            with STATE_LOCK:
                global CURRENT_CONFIG
                CURRENT_CONFIG = cfg

        flash("Configuration saved.")
        return redirect(url_for("index"))

    cfg = CURRENT_CONFIG or load_current_config()
    return render_template("config.html", cfg=cfg)


# ----------------------------
# JSON API endpoints (fake API server)
# ----------------------------

@app.route("/api/json/logs/<name>")
def api_json_logs(name: str):
    ensure_json_loaded()
    count = int(request.args.get("count", 10))
    name = secure_filename(name)
    if name not in JSON_SAMPLES:
        return {"error": "unknown dataset"}, 404
    samples = JSON_SAMPLES[name]
    # Rotate through samples to simulate ongoing variety
    start = random.randint(0, max(0, len(samples) - 1))
    out = [samples[(start + i) % len(samples)] for i in range(max(1, min(count, 1000)))]
    return Response(json.dumps(out), mimetype="application/json")


@app.route("/api/json/stream/<name>")
def api_json_stream(name: str):
    ensure_json_loaded()
    name = secure_filename(name)
    if name not in JSON_SAMPLES:
        return {"error": "unknown dataset"}, 404
    mps = float(request.args.get("mps", 1))
    period = 1.0 / max(0.1, mps)

    def gen():
        idx = 0
        while True:
            payload = JSON_SAMPLES[name][idx % len(JSON_SAMPLES[name])]
            data = json.dumps(payload)
            yield f"data: {data}\n\n"
            idx += 1
            time.sleep(period)

    return Response(gen(), mimetype="text/event-stream")


# ----------------------------
# JSON forwarder controls
# ----------------------------

@app.route("/json_forwarder/start", methods=["POST"])
def json_forwarder_start():
    name = request.form.get("json_name")
    mps = float(request.form.get("mps", 1))
    if not name:
        flash("Select a JSON dataset.")
        return redirect(url_for("index"))
    try:
        start_json_forwarder(name, mps)
        flash(f"JSON forwarder started for '{name}' at {mps} mps.")
    except Exception as e:
        flash(f"Failed to start JSON forwarder: {e}")
    return redirect(url_for("index"))


@app.route("/json_forwarder/stop", methods=["POST"])
def json_forwarder_stop():
    stop_json_forwarder()
    flash("JSON forwarder stopped.")
    return redirect(url_for("index"))


@app.route("/events")
def events():
    # Server-Sent Events endpoint
    q: Queue = Queue()
    # Seed with recent history for context
    for evt in list(EVENT_HISTORY):
        try:
            q.put_nowait(evt)
        except Exception:
            break
    SUBSCRIBERS.append(q)

    def gen():
        try:
            while True:
                try:
                    evt = q.get(timeout=15)
                except Empty:
                    # keep-alive comment
                    yield ": keep-alive\n\n"
                    continue
                data = json.dumps(evt)
                yield f"data: {data}\n\n"
        except GeneratorExit:
            pass
        finally:
            try:
                SUBSCRIBERS.remove(q)
            except ValueError:
                pass

    return Response(gen(), mimetype="text/event-stream")


@app.route("/downloads/<path:filename>")
def downloads(filename: str):
    # Serve files from current directory for convenience
    return send_from_directory(str(Path.cwd()), filename, as_attachment=True)


def create_app():
    # Lazy init for WSGI servers
    global CURRENT_CONFIG
    CURRENT_CONFIG = load_current_config()
    return app


if __name__ == "__main__":
    CURRENT_CONFIG = load_current_config()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5030)), debug=True)
