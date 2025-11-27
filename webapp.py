#!/usr/bin/env python3
import json
import os
import threading
import atexit
import signal
from collections import deque
import random
import time
import logging
import sys
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
from flask import g
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from syslog_forwarder import (
    FACILITY_MAP,
    SEVERITY_MAP,
    TailManager,
    build_sender,
    rfc5424_message,
    load_config,
)


app = Flask(__name__)
"""When running behind a reverse proxy (ingress, load balancer, etc.),
apply ProxyFix so Flask respects X-Forwarded-* headers. This ensures
generated URLs and redirects include the correct scheme, host, and
path prefix (X-Forwarded-Prefix) and prevents redirect issues.
"""
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
"""
Flask secret key and any passwords are intentionally not used.
The UI avoids session/flash features so no secret is required.
"""


# ----------------------------
# ----------------------------
# Logging
# ----------------------------

def init_logging():
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root.setLevel(level)
    # Quiet down werkzeug access logs unless debugging issues
    try:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
    except Exception:
        pass


log = logging.getLogger("webapp")


@app.before_request
def _log_request_start():
    try:
        g._t0 = time.time()
    except Exception:
        pass
    log.debug(
        "request start method=%s path=%s remote=%s qs=%s",
        request.method,
        request.path,
        request.remote_addr,
        request.query_string.decode("utf-8", errors="replace"),
    )


@app.after_request
def _log_request_end(resp: Response):
    try:
        dt = int((time.time() - getattr(g, "_t0", time.time())) * 1000)
    except Exception:
        dt = -1
    # Minimize request logging; downgrade to DEBUG and skip chatty endpoints
    if not str(request.path).startswith("/api/syslog/events"):
        log.debug(
            "request done method=%s path=%s status=%s bytes=%s dur_ms=%s",
            request.method,
            request.path,
            resp.status_code,
            resp.calculate_content_length() if hasattr(resp, "calculate_content_length") else resp.content_length,
            dt,
        )
    return resp


@app.teardown_request
def _log_request_teardown(exc):
    if exc is not None:
        log.error("request error path=%s err=%s", request.path, exc)


# ----------------------------
# Graceful shutdown (always stop background services)
# ----------------------------

def _shutdown_services(*_args):
    try:
        stop_webhook_emitter()
    except Exception:
        pass
    try:
        stop_forwarder()
    except Exception:
        pass
    try:
        log.info("application shutdown: services stopped")
    except Exception:
        pass


# Ensure cleanup on interpreter exit and common signals
atexit.register(_shutdown_services)
try:
    signal.signal(signal.SIGINT, lambda s, f: (_shutdown_services(), os._exit(0)))
    signal.signal(signal.SIGTERM, lambda s, f: (_shutdown_services(), os._exit(0)))
except Exception:
    # Signal setting may fail in non-main thread or restricted envs
    pass


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

def _reset_runtime_state():
    global TAIL_EVENTS, TAIL_SEQ, JSON_TEMPLATES, JSON_SAMPLES
    global WEBHOOK_THREAD, WEBHOOK_STOP, WEBHOOK_SENT_COUNT, WEBHOOK_ERROR_COUNT, WEBHOOK_INBOUND, WEBHOOK_SEQ
    # Live logs (lightweight, in-memory)
    TAIL_EVENTS = deque(maxlen=300)
    TAIL_SEQ = 0
    # JSON dataset state (for REST/Webhook simulators)
    JSON_TEMPLATES = {}
    JSON_SAMPLES = {}
    # Webhook mock (outbound emitter + inbound receiver)
    WEBHOOK_THREAD = None
    WEBHOOK_STOP = threading.Event()
    WEBHOOK_SENT_COUNT = 0
    WEBHOOK_ERROR_COUNT = 0
    WEBHOOK_INBOUND = deque(maxlen=200)
    WEBHOOK_SEQ = 0

_reset_runtime_state()


def tail_on_event(evt: Dict):
    # Convert tail manager events to logs (no SSE/UI stream)
    try:
        et = evt.get("type")
        if et == "start":
            # Downgrade per-file start/stop to DEBUG to reduce noise
            log.debug("tail start file=%s", evt.get("file"))
        elif et == "stop":
            log.debug("tail stop file=%s", evt.get("file"))
        elif et == "line":
            # Avoid logging line content by default; emit at DEBUG with filename only
            log.debug("tail line file=%s", evt.get("file"))
        # Append to in-memory live buffer with sequence id
        global TAIL_SEQ
        TAIL_SEQ += 1
        rec = dict(evt)
        rec["seq"] = TAIL_SEQ
        try:
            TAIL_EVENTS.appendleft(rec)
        except Exception:
            pass
    except Exception:
        pass


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
    log.info("forwarder started")


def _append_webhook_inbound(entry: Dict):
    """Append inbound webhook event with sequence id and trim buffer."""
    try:
        global WEBHOOK_SEQ
        WEBHOOK_SEQ += 1
        entry = dict(entry)
        entry["seq"] = WEBHOOK_SEQ
        WEBHOOK_INBOUND.appendleft(entry)
    except Exception:
        pass


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
    log.info("forwarder stopped")


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


# Removed JSON → Syslog forwarder implementation per request


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
    # External service bases (optional), used if services run separately
    rest_base = os.environ.get("REST_BASE_URL", "")
    webhook_base = os.environ.get("WEBHOOK_BASE_URL", "")
    # Provide facility/severity options for test send form
    facility_names = sorted(list(FACILITY_MAP.keys()))
    severity_names = sorted(list(SEVERITY_MAP.keys()))
    # Seed initial live items (small sample)
    initial_events = list(TAIL_EVENTS)[:50]
    return render_template(
        "syslog.html",
        running=is_running(),
        cfg=cfg,
        files=files,
        dest=dest,
        facility_names=facility_names,
        severity_names=severity_names,
        initial_events=initial_events,
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
    initial_inbound = list(WEBHOOK_INBOUND)[:100]
    return render_template(
        "webhook.html",
        running=is_running(),
        cfg=cfg,
        json_sets=json_sets,
        webhook_cfg=wh_cfg,
        sent_count=WEBHOOK_SENT_COUNT,
        error_count=WEBHOOK_ERROR_COUNT,
        inbound=initial_inbound,
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


@app.route("/syslog/test-send", methods=["POST"])
def syslog_test_send():
    """Send a test syslog message N times using current destination config.
    Fields:
    - message: text payload (required)
    - facility: name from FACILITY_MAP keys (optional)
    - severity: name from SEVERITY_MAP keys (optional)
    - app_name: override app name (optional)
    - count: number of times to send (optional, default 1, max 1000)
    """
    cfg = CURRENT_CONFIG or load_current_config()
    syslog_cfg = cfg.get("syslog", {})
    default_facility = (syslog_cfg.get("facility") or "user").lower()
    default_severity = (syslog_cfg.get("severity") or "info").lower()
    app_default = syslog_cfg.get("app_name") or "forwarder"
    host_name = syslog_cfg.get("host_name") or None

    msg = (request.form.get("message") or "").strip()
    fac_name = (request.form.get("facility") or default_facility).lower()
    sev_name = (request.form.get("severity") or default_severity).lower()
    app_name = (request.form.get("app_name") or app_default).strip() or app_default
    try:
        count = int(request.form.get("count") or 1)
    except Exception:
        count = 1
    if count < 1:
        count = 1
    if count > 1000:
        count = 1000

    if not msg:
        return redirect(url_for("syslog_page", error="Message+is+required"), code=303)

    facility = FACILITY_MAP.get(fac_name, FACILITY_MAP.get(default_facility, 1))
    severity = SEVERITY_MAP.get(sev_name, SEVERITY_MAP.get(default_severity, 6))
    procid = str(os.getpid())

    def _send_tests(local_cfg: Dict, n: int):
        sender = None
        try:
            sender = build_sender(local_cfg)
            for _ in range(n):
                syslog_msg = rfc5424_message(
                    msg=msg,
                    facility=facility,
                    severity=severity,
                    host_name=host_name,
                    app_name=app_name,
                    procid=procid,
                )
                try:
                    sender.send(syslog_msg)
                except Exception:
                    # drop failed sends silently
                    pass
        finally:
            try:
                if sender is not None:
                    sender.close()
            except Exception:
                pass

    threading.Thread(target=_send_tests, args=(cfg, count), daemon=True).start()
    return redirect(url_for("syslog_page", sent=count), code=303)


@app.route("/syslog/replay", methods=["POST"])
def syslog_replay():
    """Replay a selected .log file line-by-line as syslog messages.
    Accepts:
    - filename: name of the file in the data dir
    - mps: messages per second (optional, >0 to limit; blank/0 = unlimited)
    """
    filename = (request.form.get("filename") or "").strip()
    if not filename:
        return redirect(url_for("syslog_page", error="Missing+filename"), code=303)
    # Resolve path within the data directory only
    base_dir = get_data_dir()
    target = (base_dir / filename)
    try:
        # Prevent path traversal
        target.relative_to(base_dir)
    except Exception:
        return redirect(url_for("syslog_page", error="Invalid+path"), code=303)
    if not target.exists() or not target.is_file():
        return redirect(url_for("syslog_page", error="File+not+found"), code=303)

    cfg = CURRENT_CONFIG or load_current_config()
    syslog_cfg = cfg.get("syslog", {})
    facility = FACILITY_MAP.get((syslog_cfg.get("facility") or "user").lower(), 1)
    severity = SEVERITY_MAP.get((syslog_cfg.get("severity") or "info").lower(), 6)
    app_name = syslog_cfg.get("app_name") or "forwarder"
    host_name = syslog_cfg.get("host_name") or None
    procid = str(os.getpid())

    mps_raw = request.form.get("mps")
    mps = None
    try:
        if mps_raw not in (None, ""):
            mps = float(mps_raw)
            if mps <= 0:
                mps = None
    except Exception:
        mps = None
    interval = (1.0 / mps) if (mps and mps > 0) else None

    def _replay_file(pth: Path):
        sender = None
        try:
            sender = build_sender(cfg)
            log.info("replay started file=%s mps=%s", str(pth.name), (mps if mps else "unlimited"))
            with open(pth, "r", encoding="utf-8", errors="replace") as fh:
                next_send = time.perf_counter()
                sent = 0
                for line in fh:
                    msg = line.rstrip("\r\n")
                    if msg == "":
                        continue
                    try:
                        # Send raw line (file already contains syslog-formatted messages)
                        sender.send(msg)
                        log.debug("replayed line file=%s", str(pth))
                        sent += 1
                    except Exception:
                        pass
                    if interval is not None:
                        next_send += interval
                        delay = next_send - time.perf_counter()
                        if delay > 0:
                            time.sleep(delay)
        finally:
            try:
                if sender is not None:
                    sender.close()
            except Exception:
                pass
            try:
                log.info("replay completed file=%s sent=%s", str(pth.name), sent)
            except Exception:
                pass

    threading.Thread(target=_replay_file, args=(target,), daemon=True).start()
    return redirect(url_for("syslog_page", uploaded=str(filename)), code=303)


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
    # Minimal logger: single info about upload, rest at debug
    log.info("upload saved filename=%s size=%s context=%s", filename, sz, context)
    # If a .log file was uploaded for Syslog, forward each line as syslog asynchronously
    if context == "syslog" and is_log:
        try:
            cfg = CURRENT_CONFIG or load_current_config()
            syslog_cfg = cfg.get("syslog", {})
            facility = FACILITY_MAP.get((syslog_cfg.get("facility") or "user").lower(), 1)
            severity = SEVERITY_MAP.get((syslog_cfg.get("severity") or "info").lower(), 6)
            app_name = syslog_cfg.get("app_name") or "forwarder"
            host_name = syslog_cfg.get("host_name") or None
            procid = str(os.getpid())

            # Optional rate limiting via messages-per-second (mps)
            mps_raw = request.form.get("mps")
            mps = None
            try:
                if mps_raw not in (None, ""):
                    mps = float(mps_raw)
                    if mps <= 0:
                        mps = None  # treat non-positive as unlimited
            except Exception:
                mps = None
            interval = (1.0 / mps) if (mps and mps > 0) else None

            def _forward_file(pth: Path):
                sender = None
                try:
                    sender = build_sender(cfg)
                    log.info("upload replay started file=%s mps=%s", str(pth.name), (mps if mps else "unlimited"))
                    with open(pth, "r", encoding="utf-8", errors="replace") as fh:
                        next_send = time.perf_counter()
                        sent = 0
                        for line in fh:
                            msg = line.rstrip("\r\n")
                            if msg == "":
                                continue
                            try:
                                # Send raw line (file already contains syslog-formatted messages)
                                sender.send(msg)
                                log.debug("forwarded line file=%s", str(pth))
                                sent += 1
                            except Exception:
                                # Drop line if sending fails
                                pass
                            # Rate control: simple pacing based on mps
                            if interval is not None:
                                next_send += interval
                                # Sleep until the scheduled time; skip if we're behind
                                delay = next_send - time.perf_counter()
                                if delay > 0:
                                    time.sleep(delay)
                finally:
                    try:
                        if sender is not None:
                            sender.close()
                    except Exception:
                        pass
                    try:
                        log.info("upload replay completed file=%s sent=%s", str(pth.name), sent)
                    except Exception:
                        pass

            threading.Thread(target=_forward_file, args=(target,), daemon=True).start()
        except Exception as e:
            log.error("upload forwarding error file=%s err=%s", str(target), e)
    # Route back to originating section with success indicator
    return redirect(f"{landing_for(context)}?uploaded={filename}", code=303)


@app.route("/config", methods=["GET", "POST"])
def config_view():
    # Disable config editing in the UI
    if request.method == "POST":
        return ("Config editing is disabled in this UI.", 403)
    return ("Not found", 404)


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


def _resolve_keyword(obj, key: str):
    """Resolve a dotted key into obj. Returns the value or None.
    If the resolved value is a dict, try common list fields.
    """
    if not key:
        return obj
    cur = obj
    parts = [p for p in str(key).split('.') if p]
    try:
        for p in parts:
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                return None
    except Exception:
        return None
    # If dict, try to pick a list-like field
    if isinstance(cur, dict):
        candidates = ['logs', 'items', 'events', 'entries', 'data']
        for k in candidates:
            v = cur.get(k)
            if isinstance(v, list):
                return v
        # else pick the first list field if any
        for v in cur.values():
            if isinstance(v, list):
                return v
    return cur


@app.route("/api/json/quick/<name>")
def api_json_quick(name: str):
    """Quick tester: extract a list using a keyword path and return real or fake entries.
    Query params:
    - key: dotted path to a list or dict containing a list (e.g., 'logs' or 'data.logs')
    - count: number of entries to return (default 10)
    - mode: 'real' or 'fake' (default 'fake')
    """
    name = secure_filename(name)
    try:
        count = max(1, min(1000, int(request.args.get('count', 10))))
    except Exception:
        count = 10
    mode = (request.args.get('mode') or 'fake').strip().lower()
    raw_flag = (request.args.get('raw') or '').strip().lower()
    raw = raw_flag in ('1', 'true', 'yes', 'on')
    key = (request.args.get('key') or '').strip()

    # Load raw dataset file
    ddir = get_data_dir() / 'datasets'
    src = ddir / f"{name}.json"
    if not src.exists():
        return {"error": "unknown dataset"}, 404
    try:
        with open(src, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        return {"error": "failed to read dataset"}, 500

    value = _resolve_keyword(data, key) if key else data
    # If list is wrapped inside dict without explicit key and data is a list, use it directly
    lst = None
    if isinstance(value, list):
        lst = value
    elif isinstance(value, dict):
        # Heuristic already tried; fall back: take first list field
        for v in value.values():
            if isinstance(v, list):
                lst = v
                break
    if lst is None:
        # If no list, treat the resolved value as a single object
        base = value
        if mode == 'real':
            # Return at most one real entry if it's an object; otherwise wrap
            if isinstance(base, dict):
                items = [base][:count]
            else:
                items = [{"value": base}][:count]
            if raw:
                return Response(json.dumps(items), mimetype="application/json")
            else:
                resp = {
                    "mode": mode,
                    "requested": count,
                    "returned": len(items),
                    "total_available": len(items),
                    "items": items,
                }
                return Response(json.dumps(resp), mimetype="application/json")
        else:
            # Fake: generate from the object (or wrap scalar)
            if not isinstance(base, dict):
                base = {"value": base}
            items = generate_samples_from_template(base, count)
            if raw:
                return Response(json.dumps(items), mimetype="application/json")
            else:
                resp = {
                    "mode": mode,
                    "requested": count,
                    "returned": len(items),
                    "total_available": len(items),
                    "items": items,
                }
                return Response(json.dumps(resp), mimetype="application/json")

    total = len(lst)
    items = []
    if mode == 'real':
        items = lst[:count]
    else:
        # fake: generate mutated samples from the first element (or empty dict)
        base = lst[0] if lst else {}
        if isinstance(base, dict):
            items = generate_samples_from_template(base, count)
        else:
            # If base is not a dict, wrap in dict under 'value'
            items = generate_samples_from_template({"value": base}, count)
    if raw:
        return Response(json.dumps(items), mimetype="application/json")
    else:
        resp = {
            "mode": mode,
            "requested": count,
            "returned": len(items),
            "total_available": total,
            "items": items,
        }
        return Response(json.dumps(resp), mimetype="application/json")

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


# JSON → Syslog forwarder routes removed


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
            code = resp.getcode()
            log.debug("http post ok url=%s code=%s", url, code)
            return code
    except urllib.error.HTTPError as e:
        log.error("http error url=%s code=%s", url, getattr(e, 'code', None))
        return getattr(e, 'code', None)
    except urllib.error.URLError as e:
        # Connection errors, DNS, refused, timeouts, etc.
        log.error("http urlerror url=%s reason=%s", url, getattr(e, 'reason', e))
        return None
    except Exception as e:
        log.exception("http unexpected error url=%s err=%s", url, e)
        return None


def start_webhook_emitter(url: str, name: str, mps: float = 1.0, method: str = "POST", headers: Dict | None = None, copies: int = 1, interval_sec: float | None = None, max_events: int | None = None):
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
        nonlocal url, name, mps, method, headers, copies, interval_sec, max_events
        try:
            log.info("webhook emitter started name=%s interval_sec=%s count=%s url=%s", name, interval_sec, max_events, url)
            idx = 0
            period = float(interval_sec) if interval_sec is not None else 1.0
            samples = JSON_SAMPLES[name]
            remaining = int(max_events) if max_events is not None else None
            while not WEBHOOK_STOP.is_set():
                payload = samples[idx % len(samples)]
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                code = _http_post(url, body, headers=headers, method=method)
                if code and 200 <= int(code) < 400:
                    globals()["WEBHOOK_SENT_COUNT"] += 1
                else:
                    globals()["WEBHOOK_ERROR_COUNT"] += 1
                idx += 1
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        break
                time.sleep(period)
        finally:
            log.info("webhook emitter stopped name=%s", name)

    WEBHOOK_THREAD = threading.Thread(target=run, daemon=True)
    WEBHOOK_THREAD.start()


def stop_webhook_emitter():
    WEBHOOK_STOP.set()
    if WEBHOOK_THREAD is not None:
        WEBHOOK_THREAD.join(timeout=2.0)


def start_webhook_emitter_samples(url: str, samples: list, mps: float = 1.0, method: str = "POST", headers: Dict | None = None, tag: str = "adhoc", copies: int = 1, interval_sec: float | None = None, max_events: int | None = None):
    """Start a webhook emitter using an explicit samples list instead of a named dataset."""
    global WEBHOOK_THREAD, WEBHOOK_SENT_COUNT, WEBHOOK_ERROR_COUNT
    if WEBHOOK_THREAD is not None and WEBHOOK_THREAD.is_alive():
        return

    WEBHOOK_STOP.clear()
    WEBHOOK_SENT_COUNT = 0
    WEBHOOK_ERROR_COUNT = 0

    def run():
        nonlocal url, samples, mps, method, headers, tag, copies, interval_sec, max_events
        try:
            log.info("webhook emitter started name=%s interval_sec=%s count=%s url=%s", tag, interval_sec, max_events, url)
            idx = 0
            period = float(interval_sec) if interval_sec is not None else 1.0
            remaining = int(max_events) if max_events is not None else None
            while not WEBHOOK_STOP.is_set():
                payload = samples[idx % len(samples)]
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                code = _http_post(url, body, headers=headers, method=method)
                if code and 200 <= int(code) < 400:
                    globals()["WEBHOOK_SENT_COUNT"] += 1
                else:
                    globals()["WEBHOOK_ERROR_COUNT"] += 1
                idx += 1
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        break
                time.sleep(period)
        finally:
            log.info("webhook emitter stopped name=%s", tag)

    WEBHOOK_THREAD = threading.Thread(target=run, daemon=True)
    WEBHOOK_THREAD.start()


@app.route("/api/webhook/incoming", methods=["POST","PUT"])
def webhook_incoming():
    # Capture inbound webhook calls for testing
    # Optional API key enforcement via env
    expected = os.environ.get("LPHC_API_KEY") or os.environ.get("API_KEY")
    if expected:
        if (request.headers.get("x-api-key") or "") != expected:
            return Response(json.dumps({"message": "Unauthorized"}), status=401, mimetype="application/json")
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
    _append_webhook_inbound(entry)
    log.info("webhook received path=%s", request.path)
    return {"message": "Request received successfully"}


# Alias path to match Logpoint receiver flow
@app.route("/lphc/events/json", methods=["POST"])  # matches: POST /lphc/events/json
def webhook_incoming_logpoint():
    # Optional API key enforcement via env
    expected = os.environ.get("LPHC_API_KEY") or os.environ.get("API_KEY")
    if expected:
        if (request.headers.get("x-api-key") or "") != expected:
            return Response(json.dumps({"message": "Unauthorized"}), status=401, mimetype="application/json")
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
    _append_webhook_inbound(entry)
    log.info("webhook received path=%s", request.path)
    # Respond with 200 OK and JSON
    return Response(json.dumps({"message": "Request received successfully"}), mimetype="application/json")


@app.route("/lphc/events/xml", methods=["POST"])  # Receives XML data
def webhook_incoming_logpoint_xml():
    expected = os.environ.get("LPHC_API_KEY") or os.environ.get("API_KEY")
    if expected:
        if (request.headers.get("x-api-key") or "") != expected:
            return Response(json.dumps({"message": "Unauthorized"}), status=401, mimetype="application/json")
    try:
        body = request.data.decode("utf-8", errors="replace")
    except Exception:
        body = ""
    entry = {
        "headers": {k: v for k, v in request.headers.items()},
        "xml": body,
        "args": request.args.to_dict(flat=True),
        "path": request.path,
        "ts": int(time.time()),
    }
    _append_webhook_inbound(entry)
    log.info("webhook received path=%s", request.path)
    return Response(json.dumps({"message": "Request received successfully"}), mimetype="application/json")


@app.route("/lphc/events", methods=["POST"])  # Receives raw data
def webhook_incoming_logpoint_raw():
    expected = os.environ.get("LPHC_API_KEY") or os.environ.get("API_KEY")
    if expected:
        if (request.headers.get("x-api-key") or "") != expected:
            return Response(json.dumps({"message": "Unauthorized"}), status=401, mimetype="application/json")
    content_type = request.headers.get("Content-Type", "")
    payload = None
    try:
        if content_type.startswith("application/json"):
            payload = request.get_json(silent=True)
        if payload is None:
            payload = request.data.decode("utf-8", errors="replace")
    except Exception:
        payload = request.data.decode("utf-8", errors="replace")
    entry = {
        "headers": {k: v for k, v in request.headers.items()},
        "payload": payload,
        "content_type": content_type,
        "args": request.args.to_dict(flat=True),
        "path": request.path,
        "ts": int(time.time()),
    }
    _append_webhook_inbound(entry)
    log.info("webhook received path=%s", request.path)
    return Response(json.dumps({"message": "Request received successfully"}), mimetype="application/json")


@app.route("/lphc/health", methods=["GET"])  # Health check endpoint
def lphc_health_ui():
    return Response(json.dumps({"status": "ok"}), mimetype="application/json")


@app.route("/webhook/start", methods=["POST"])
def webhook_start():
    cfg = CURRENT_CONFIG or load_current_config()
    name = request.form.get("json_name")
    url_ = request.form.get("webhook_url") or cfg.get("webhook", {}).get("url")
    # Only two controls: count and interval_sec
    try:
        count = int(request.form.get("count") or 1)
        if count < 1:
            count = 1
        if count > 100000:
            count = 100000
    except Exception:
        count = 1
    interval_raw = request.form.get("interval_sec")
    try:
        interval_sec = float(interval_raw) if interval_raw not in (None, "") else None
        if interval_sec is not None and interval_sec <= 0:
            interval_sec = 1.0
    except Exception:
        interval_sec = 1.0
    method = (request.form.get("method") or cfg.get("webhook", {}).get("method", "POST")).upper()
    # Do NOT read headers from config to avoid shipping secrets; headers only from user input
    hdrs_raw = request.form.get("headers")
    hdrs = {}
    if isinstance(hdrs_raw, dict):
        hdrs = hdrs_raw
    elif isinstance(hdrs_raw, str) and hdrs_raw.strip():
        try:
            hdrs = json.loads(hdrs_raw)
        except Exception:
            hdrs = {}
    # If no JSON headers provided, build from key-value arrays (hdr_name[], hdr_value[])
    if not hdrs:
        names = request.form.getlist("hdr_name[]")
        values = request.form.getlist("hdr_value[]")
        if names and values:
            for k, v in zip(names, values):
                k = (k or "").strip()
                v = (v or "").strip()
                if k:
                    hdrs[k] = v
    # Logpoint quick preset support
    lp_host = (request.form.get("lp_host") or "").strip()
    lp_scheme = (request.form.get("lp_scheme") or "http").strip()
    lp_api_key = (request.form.get("lp_api_key") or "").strip()
    if lp_host:
        # Build URL in the expected format: {scheme}://{host}/lphc/events/json
        if lp_host.startswith("http://") or lp_host.startswith("https://"):
            base = lp_host
        else:
            base = f"{lp_scheme}://{lp_host}"
        if not base.endswith("/"):
            base = base + "/"
        url_ = base + "lphc/events/json"
        # Apply x-api-key header if provided
        if lp_api_key and "x-api-key" not in {k.lower(): v for k, v in hdrs.items()}:
            hdrs["x-api-key"] = lp_api_key

    # Raw JSON template (adhoc) support
    raw_json = request.form.get("raw_json")
    adhoc_samples = None
    if raw_json and raw_json.strip():
        try:
            raw = json.loads(raw_json)
            if isinstance(raw, list) and raw:
                seed = raw[0]
            else:
                seed = raw
            if isinstance(seed, dict):
                adhoc_samples = generate_samples_from_template(seed, 500)
        except Exception:
            adhoc_samples = None
    if not name or not url_:
        # If adhoc samples exist, allow start without named dataset
        if adhoc_samples and url_:
            try:
                start_webhook_emitter_samples(url_, adhoc_samples, method=method, headers=hdrs, tag="adhoc", interval_sec=interval_sec, max_events=count)
            except Exception:
                pass
        else:
            return redirect(url_for("webhook_page"), code=303)
    else:
        try:
            if adhoc_samples:
                start_webhook_emitter_samples(url_, adhoc_samples, method=method, headers=hdrs, tag=f"adhoc:{name}", interval_sec=interval_sec, max_events=count)
            else:
                start_webhook_emitter(url_, name, method=method, headers=hdrs, interval_sec=interval_sec, max_events=count)
        except Exception:
            pass
    return redirect(url_for("webhook_page"), code=303)


@app.route("/webhook/stop", methods=["POST"])
def webhook_stop():
    stop_webhook_emitter()
    return redirect(url_for("webhook_page"), code=303)


@app.route("/lphc/schedule", methods=["POST"])  # Accept JSON or file to schedule outbound webhook
def lphc_schedule():
    # Optional API key enforcement via env
    expected = os.environ.get("LPHC_API_KEY") or os.environ.get("API_KEY")
    if expected:
        if (request.headers.get("x-api-key") or "") != expected:
            return Response(json.dumps({"message": "Unauthorized"}), status=401, mimetype="application/json")

    # Determine samples from JSON body or uploaded file
    samples = None
    # Multipart file takes precedence
    up = request.files.get("file")
    if up and up.filename:
        try:
            raw = up.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, list):
                samples = data
            elif isinstance(data, dict):
                # Allow {"samples":[...]}
                if isinstance(data.get("samples"), list):
                    samples = data.get("samples")
                else:
                    samples = [data]
        except Exception:
            samples = None
    if samples is None:
        body = request.get_json(silent=True)
        if isinstance(body, list):
            samples = body
        elif isinstance(body, dict) and body:
            if isinstance(body.get("samples"), list):
                samples = body.get("samples")
            else:
                samples = [body]

    if not samples:
        return Response(json.dumps({"message": "Invalid or empty JSON payload"}), status=400, mimetype="application/json")

    # Determine rate and target
    cfg = CURRENT_CONFIG or load_current_config()
    # rate via mps or interval_sec
    interval_sec = request.args.get("interval_sec") or (request.json or {}).get("interval_sec") if request.is_json else None
    mps_q = request.args.get("mps")
    try:
        if interval_sec is not None:
            interval = float(interval_sec)
            mps = 1.0 / max(0.001, interval)
        elif mps_q is not None:
            mps = float(mps_q)
        else:
            mps = float(cfg.get("webhook", {}).get("mps", 1.0))
    except Exception:
        mps = 1.0

    # Copies per tick
    try:
        copies = int(request.args.get("copies") or ((request.json or {}).get("copies") if request.is_json else None) or 1)
        if copies < 1:
            copies = 1
    except Exception:
        copies = 1

    # target URL and headers
    target_url = request.args.get("url") or (request.json or {}).get("target_url") if request.is_json else None
    if not target_url:
        target_url = cfg.get("webhook", {}).get("url")
    if not target_url:
        return Response(json.dumps({"message": "Missing target URL"}), status=400, mimetype="application/json")

    # No default headers from config; allow override x-api-key via query/body only
    headers_cfg = {}
    # allow override x-api-key via query/body
    override_api_key = request.args.get("api_key") or ((request.json or {}).get("api_key") if request.is_json else None)
    hdrs = dict(headers_cfg)
    if override_api_key:
        hdrs["x-api-key"] = override_api_key

    try:
        start_webhook_emitter_samples(target_url, samples, mps=mps, method="POST", headers=hdrs, tag="schedule", copies=copies, interval_sec=float(interval_sec) if interval_sec else None)
    except Exception as e:
        return Response(json.dumps({"message": f"Failed to start schedule: {e}"}), status=500, mimetype="application/json")

    return Response(json.dumps({"message": "Schedule started", "mps": mps}), mimetype="application/json")


# SSE /events removed; use logs for visibility


@app.route("/downloads/<path:filename>")
def downloads(filename: str):
    # Serve files from the data directory for convenience
    return send_from_directory(str(get_data_dir()), filename, as_attachment=True)


# ----------------------------
# Live feeds (JSON)
# ----------------------------

@app.route("/api/syslog/events")
def api_syslog_events():
    try:
        since = int(request.args.get("since", 0))
    except Exception:
        since = 0
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except Exception:
        limit = 50
    events = []
    max_seq = since
    for e in list(TAIL_EVENTS):
        s = int(e.get("seq", 0))
        if s > max_seq:
            max_seq = s
        if s > since:
            events.append(e)
        if len(events) >= limit:
            break
    return Response(json.dumps({"events": list(reversed(events)), "last_seq": max_seq}), mimetype="application/json")


@app.route("/api/webhook/inbound_feed")
def api_webhook_inbound_feed():
    try:
        since = int(request.args.get("since", 0))
    except Exception:
        since = 0
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except Exception:
        limit = 50
    items = []
    max_seq = since
    for e in list(WEBHOOK_INBOUND):
        s = int(e.get("seq", 0))
        if s > max_seq:
            max_seq = s
        if s > since:
            items.append(e)
        if len(items) >= limit:
            break
    return Response(json.dumps({"items": list(reversed(items)), "last_seq": max_seq}), mimetype="application/json")

def create_app():
    # Lazy init for WSGI servers
    global CURRENT_CONFIG
    init_logging()
    get_data_dir()  # ensure dirs exist early
    CURRENT_CONFIG = load_current_config()
    log.info("webapp started")
    return app


if __name__ == "__main__":
    init_logging()
    get_data_dir()
    CURRENT_CONFIG = load_current_config()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5030)), debug=True)
