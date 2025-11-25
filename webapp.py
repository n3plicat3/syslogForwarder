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
"""
Flask secret key and any passwords are intentionally not used.
The UI avoids session/flash features so no secret is required.
"""


# ----------------------------
# Global state
# ----------------------------

STATE_LOCK = threading.Lock()
MANAGER_THREAD: threading.Thread | None = None
TAIL_MANAGER: TailManager | None = None
CURRENT_CONFIG: Dict = {}

# Data directories
DATA_DIR: Path | None = None
DATASETS_DIR: Path | None = None

# Event broadcasting (simple in-process pub/sub)
SUBSCRIBERS: List[Queue] = []
EVENT_HISTORY = deque(maxlen=500)

# JSON simulation/forwarder state
JSON_TEMPLATES: Dict[str, dict] = {}
JSON_SAMPLES: Dict[str, list] = {}
JSON_FORWARDER_THREAD: threading.Thread | None = None
JSON_FORWARDER_STOP = threading.Event()
JSON_FORWARDER_CFG: Dict = {}

# Webhook mock (outbound emitter + inbound receiver)
WEBHOOK_THREAD: threading.Thread | None = None
WEBHOOK_STOP = threading.Event()
WEBHOOK_SENT_COUNT = 0
WEBHOOK_ERROR_COUNT = 0
WEBHOOK_INBOUND = deque(maxlen=200)


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

        directory = get_data_dir()
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
# Data dir helpers + JSON helpers
# ----------------------------

def get_data_dir() -> Path:
    """Resolve the directory where files and uploads live.
    Preference order:
    1) $DATA_DIR if set
    2) /data if it exists
    3) ./data if it exists (or can be created)
    4) current working directory
    """
    global DATA_DIR, DATASETS_DIR
    if DATA_DIR is not None:
        return DATA_DIR

    candidates = []
    env_dir = os.environ.get("DATA_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend([Path("/data"), Path.cwd() / "data", Path.cwd()])

    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            DATA_DIR = p
            break
        except Exception:
            continue
    if DATA_DIR is None:
        DATA_DIR = Path.cwd()

    # Ensure datasets subdir exists
    DATASETS_DIR = DATA_DIR / "datasets"
    try:
        DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return DATA_DIR

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
    """Load .json files from datasets dir as templates if not loaded.
    Avoids treating arbitrary JSON (e.g., config.json) as datasets.
    """
    ddir = get_data_dir()
    ds_dir = (ddir / "datasets")
    try:
        ds_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    for p in ds_dir.glob("*.json"):
        name = p.stem
        if name in JSON_TEMPLATES:
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                JSON_TEMPLATES[name] = data
                JSON_SAMPLES[name] = generate_samples_from_template(data, 500)
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
    # Serve Syslog page directly to avoid redirect loops (200 OK)
    return syslog_page()


@app.route("/syslog")
def syslog_page():
    cfg = CURRENT_CONFIG or load_current_config()
    files_cfg = cfg.get("files", {})
    pattern = files_cfg.get("pattern", "*.log")
    directory = get_data_dir()
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
    # External service bases (optional), used if services run separately
    rest_base = os.environ.get("REST_BASE_URL", "")
    webhook_base = os.environ.get("WEBHOOK_BASE_URL", "")
    return render_template(
        "syslog.html",
        running=is_running(),
        cfg=cfg,
        files=files,
        dest=dest,
        json_sets=json_sets,
        rest_base=rest_base,
        webhook_base=webhook_base,
        section="syslog",
    )


@app.route("/rest")
def rest_page():
    cfg = CURRENT_CONFIG or load_current_config()
    ensure_json_loaded()
    json_sets = sorted(list(JSON_TEMPLATES.keys()))
    rest_base = os.environ.get("REST_BASE_URL", "")
    webhook_base = os.environ.get("WEBHOOK_BASE_URL", "")
    return render_template(
        "rest.html",
        running=is_running(),
        cfg=cfg,
        json_sets=json_sets,
        rest_base=rest_base,
        webhook_base=webhook_base,
        section="rest",
    )


@app.route("/webhook")
def webhook_page():
    cfg = CURRENT_CONFIG or load_current_config()
    ensure_json_loaded()
    json_sets = sorted(list(JSON_TEMPLATES.keys()))
    wh_cfg = cfg.get("webhook", {})
    rest_base = os.environ.get("REST_BASE_URL", "")
    webhook_base = os.environ.get("WEBHOOK_BASE_URL", "")
    return render_template(
        "webhook.html",
        running=is_running(),
        cfg=cfg,
        json_sets=json_sets,
        webhook_cfg=wh_cfg,
        sent_count=WEBHOOK_SENT_COUNT,
        error_count=WEBHOOK_ERROR_COUNT,
        inbound=list(WEBHOOK_INBOUND),
        rest_base=rest_base,
        webhook_base=webhook_base,
        section="webhook",
    )


@app.route("/start", methods=["POST"])
def start():
    cfg = CURRENT_CONFIG or load_current_config()
    start_forwarder(cfg)
    return redirect(url_for("syslog_page"), code=303)


@app.route("/stop", methods=["POST"])
def stop():
    stop_forwarder()
    return redirect(url_for("syslog_page"), code=303)


@app.route("/upload", methods=["POST"])
def upload():
    # Enforce upload type based on origin context
    context = (request.form.get("context") or "").strip().lower()
    def landing_for(ctx: str) -> str:
        if ctx == "rest":
            return url_for("rest_page")
        if ctx == "webhook":
            return url_for("webhook_page")
        return url_for("syslog_page")

    f = request.files.get("file")
    if not f or f.filename == "":
        return redirect(f"{landing_for(context)}?error=No+file+selected", code=303)
    filename = secure_filename(f.filename or "")
    if not filename:
        return redirect(f"{landing_for(context)}?error=Invalid+filename", code=303)

    base_dir = get_data_dir()
    is_json = filename.lower().endswith(".json")
    is_log = filename.lower().endswith(".log")

    # Validate allowed extensions by context
    if context == "syslog" and not is_log:
        return redirect(f"{landing_for(context)}?error=Only+.log+files+are+allowed+for+Syslog", code=303)
    if context in ("rest", "webhook") and not is_json:
        return redirect(f"{landing_for(context)}?error=Only+.json+files+are+allowed+for+REST/Webhook", code=303)
    # JSON uploads go under datasets/, others at root data dir
    target_dir = (base_dir / "datasets") if is_json else base_dir
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    target = target_dir / filename
    f.save(str(target))
    try:
        sz = target.stat().st_size
    except Exception:
        sz = None
    # If JSON file, load as template and generate default samples
    if is_json:
        parse_ok = False
        try:
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                name = Path(filename).stem
                JSON_TEMPLATES[name] = data
                JSON_SAMPLES[name] = generate_samples_from_template(data, 100)
                parse_ok = True
        except Exception:
            parse_ok = False
        # If upload originated from REST/Webhook, invalid JSON should be reported
        if context in ("rest", "webhook") and not parse_ok:
            try:
                target.unlink(missing_ok=True)  # remove invalid file
            except Exception:
                pass
            return redirect(f"{landing_for(context)}?error=Invalid+JSON+file+format", code=303)
    # Emit an event so the UI can show upload confirmation in Live Log
    try:
        broadcast_event({"type": "upload", "filename": filename, "size": sz})
    except Exception:
        pass
    # Route back to originating section
    return redirect(landing_for(context), code=303)


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

        # Webhook settings
        webhook = cfg.setdefault("webhook", {})
        webhook["url"] = request.form.get("webhook_url") or webhook.get("url")
        webhook["method"] = (request.form.get("webhook_method") or webhook.get("method") or "POST").upper()
        # Headers as JSON dict string; fallback to previous dict
        headers_raw = request.form.get("webhook_headers")
        if headers_raw:
            try:
                webhook["headers"] = json.loads(headers_raw)
            except Exception:
                webhook["headers"] = webhook.get("headers") or {}
        webhook["mps"] = float(request.form.get("webhook_mps", webhook.get("mps", 1)))

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

        # Redirect back to section if provided
        next_section = request.form.get("next") or "syslog"
        if next_section == "webhook":
            return redirect(url_for("webhook_page"), code=303)
        if next_section == "rest":
            return redirect(url_for("rest_page"), code=303)
        return redirect(url_for("syslog_page"), code=303)

    cfg = CURRENT_CONFIG or load_current_config()
    return render_template("config.html", cfg=cfg, running=is_running(), section="config")


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
# REST mock with pagination
# ----------------------------

@app.route("/api/rest/<name>")
def api_rest_paginated(name: str):
    ensure_json_loaded()
    name = secure_filename(name)
    if name not in JSON_SAMPLES:
        return {"error": "unknown dataset"}, 404
    # Pagination params
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    try:
        per_page = max(1, min(200, int(request.args.get("per_page", 20))))
    except Exception:
        per_page = 20

    samples = JSON_SAMPLES[name]
    total = len(samples)
    start = (page - 1) * per_page
    end = start + per_page
    items = samples[start:end]
    # If page is beyond range, return empty list
    if start >= total:
        items = []

    base = url_for("api_rest_paginated", name=name, _external=False)
    def link(p):
        return f"{base}?page={p}&per_page={per_page}"

    resp = {
        "items": items,
        "meta": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": (total + per_page - 1) // per_page,
        },
        "links": {
            "self": link(page),
            "next": link(page + 1) if end < total else None,
            "prev": link(page - 1) if page > 1 else None,
        },
    }
    return Response(json.dumps(resp), mimetype="application/json")


# ----------------------------
# JSON forwarder controls
# ----------------------------

@app.route("/json_forwarder/start", methods=["POST"])
def json_forwarder_start():
    name = request.form.get("json_name")
    mps = float(request.form.get("mps", 1))
    if not name:
        return redirect(url_for("syslog_page"), code=303)
    try:
        start_json_forwarder(name, mps)
    except Exception as e:
        # Start failed; just fall through to index
        pass
    return redirect(url_for("syslog_page"), code=303)


@app.route("/json_forwarder/stop", methods=["POST"])
def json_forwarder_stop():
    stop_json_forwarder()
    return redirect(url_for("syslog_page"), code=303)


# ----------------------------
# Webhook mock: outbound emitter and inbound receiver
# ----------------------------

def _http_post(url: str, body: bytes, headers: Dict[str, str] | None = None, method: str = "POST"):
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url=url, data=body, method=method.upper())
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update({str(k): str(v) for k, v in headers.items()})
    for k, v in hdrs.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.getcode()
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def start_webhook_emitter(url: str, name: str, mps: float = 1.0, method: str = "POST", headers: Dict | None = None):
    global WEBHOOK_THREAD, WEBHOOK_SENT_COUNT, WEBHOOK_ERROR_COUNT
    ensure_json_loaded()
    if name not in JSON_SAMPLES:
        raise ValueError("Unknown json template name")
    if WEBHOOK_THREAD is not None and WEBHOOK_THREAD.is_alive():
        return

    WEBHOOK_STOP.clear()
    WEBHOOK_SENT_COUNT = 0
    WEBHOOK_ERROR_COUNT = 0

    def run():
        nonlocal url, name, mps, method, headers
        try:
            broadcast_event({"type": "webhook", "status": "started", "name": name, "mps": mps, "url": url})
            idx = 0
            period = 1.0 / max(0.1, float(mps))
            samples = JSON_SAMPLES[name]
            while not WEBHOOK_STOP.is_set():
                payload = samples[idx % len(samples)]
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                code = _http_post(url, body, headers=headers, method=method)
                if code and 200 <= int(code) < 400:
                    broadcast_event({"type": "webhook_sent", "code": code, "name": name})
                    globals()["WEBHOOK_SENT_COUNT"] += 1
                else:
                    broadcast_event({"type": "webhook_error", "code": code, "name": name})
                    globals()["WEBHOOK_ERROR_COUNT"] += 1
                idx += 1
                time.sleep(period)
        finally:
            broadcast_event({"type": "webhook", "status": "stopped", "name": name})

    WEBHOOK_THREAD = threading.Thread(target=run, daemon=True)
    WEBHOOK_THREAD.start()


def stop_webhook_emitter():
    WEBHOOK_STOP.set()
    if WEBHOOK_THREAD is not None:
        WEBHOOK_THREAD.join(timeout=2.0)


@app.route("/api/webhook/incoming", methods=["POST","PUT"])
def webhook_incoming():
    # Capture inbound webhook calls for testing
    try:
        data = request.get_json(silent=True)
    except Exception:
        data = None
    entry = {
        "headers": {k: v for k, v in request.headers.items()},
        "json": data,
        "args": request.args.to_dict(flat=True),
        "path": request.path,
        "ts": int(time.time()),
    }
    WEBHOOK_INBOUND.appendleft(entry)
    broadcast_event({"type": "webhook_received", "path": request.path})
    return {"status": "ok"}


@app.route("/webhook/start", methods=["POST"])
def webhook_start():
    cfg = CURRENT_CONFIG or load_current_config()
    name = request.form.get("json_name")
    url_ = request.form.get("webhook_url") or cfg.get("webhook", {}).get("url")
    mps = float(request.form.get("mps", cfg.get("webhook", {}).get("mps", 1)))
    method = (request.form.get("method") or cfg.get("webhook", {}).get("method", "POST")).upper()
    hdrs_raw = request.form.get("headers") or cfg.get("webhook", {}).get("headers")
    hdrs = {}
    if isinstance(hdrs_raw, dict):
        hdrs = hdrs_raw
    elif isinstance(hdrs_raw, str) and hdrs_raw.strip():
        try:
            hdrs = json.loads(hdrs_raw)
        except Exception:
            hdrs = {}
    if not name or not url_:
        return redirect(url_for("webhook_page"), code=303)
    try:
        start_webhook_emitter(url_, name, mps=mps, method=method, headers=hdrs)
    except Exception:
        pass
    return redirect(url_for("webhook_page"), code=303)


@app.route("/webhook/stop", methods=["POST"])
def webhook_stop():
    stop_webhook_emitter()
    return redirect(url_for("webhook_page"), code=303)


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
    # Serve files from the data directory for convenience
    return send_from_directory(str(get_data_dir()), filename, as_attachment=True)


def create_app():
    # Lazy init for WSGI servers
    global CURRENT_CONFIG
    get_data_dir()  # ensure dirs exist early
    CURRENT_CONFIG = load_current_config()
    return app


if __name__ == "__main__":
    get_data_dir()
    CURRENT_CONFIG = load_current_config()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5030)), debug=True)
