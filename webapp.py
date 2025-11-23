#!/usr/bin/env python3
import json
import os
import threading
from collections import deque
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


@app.route("/")
def index():
    cfg = CURRENT_CONFIG or load_current_config()
    files_cfg = cfg.get("files", {})
    pattern = files_cfg.get("pattern", "*.log")
    directory = Path.cwd()
    files = sorted([str(p.name) for p in directory.glob(pattern)])
    dest = cfg.get("destination", {})
    return render_template(
        "index.html",
        running=is_running(),
        cfg=cfg,
        files=files,
        dest=dest,
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
    flash(f"Uploaded {filename}.")
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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

