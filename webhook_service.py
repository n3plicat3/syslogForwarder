#!/usr/bin/env python3
import os
import time
import logging
import sys
from collections import deque
from typing import Deque, Dict

from flask import Flask, request, Response, g

app = Flask(__name__)


# ----------------------------
# Logging
# ----------------------------

def init_logging():
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        ))
        root.addHandler(h)
    root.setLevel(level)


log = logging.getLogger("webhook_service")


@app.before_request
def _log_start():
    try:
        g._t0 = time.time()
    except Exception:
        pass
    log.debug("request start method=%s path=%s remote=%s", request.method, request.path, request.remote_addr)


@app.after_request
def _log_end(resp: Response):
    try:
        dt = int((time.time() - getattr(g, "_t0", time.time())) * 1000)
    except Exception:
        dt = -1
    log.info("request done method=%s path=%s status=%s dur_ms=%s", request.method, request.path, resp.status_code, dt)
    return resp


@app.teardown_request
def _log_teardown(exc):
    if exc is not None:
        log.error("request error path=%s err=%s", request.path, exc)

INBOUND: Deque[Dict] = deque(maxlen=500)


def _require_api_key() -> tuple[bool, Response | None]:
    """Enforce x-api-key if LPHC_API_KEY (or API_KEY) env var is set.
    Returns (ok, response) where response is a 401 if unauthorized.
    """
    expected = os.environ.get("LPHC_API_KEY") or os.environ.get("API_KEY")
    if not expected:
        return True, None
    got = request.headers.get("x-api-key")
    if got == expected:
        return True, None
    log.error("unauthorized request path=%s remote=%s", request.path, request.remote_addr)
    return False, Response(
        response='{"message":"Unauthorized"}',
        status=401,
        mimetype="application/json",
    )


@app.route("/api/webhook/incoming", methods=["POST", "PUT"])
def webhook_incoming():
    ok, resp = _require_api_key()
    if not ok:
        return resp
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
    INBOUND.appendleft(entry)
    log.info("webhook received path=%s has_json=%s", request.path, data is not None)
    return {"message": "Request received successfully"}


@app.route("/status")
def status():
    cnt = len(INBOUND)
    log.debug("status requested inbound_count=%s", cnt)
    return {"inbound_count": cnt}


# Alias path to match Logpoint receiver flow
@app.route("/lphc/events/json", methods=["POST"])  # matches: POST /lphc/events/json
def webhook_incoming_logpoint():
    ok, resp = _require_api_key()
    if not ok:
        return resp
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
    INBOUND.appendleft(entry)
    log.info("webhook received (logpoint) path=%s has_json=%s", request.path, data is not None)
    return {"message": "Request received successfully"}


@app.route("/lphc/events/xml", methods=["POST"])  # Receives XML data
def webhook_incoming_xml():
    ok, resp = _require_api_key()
    if not ok:
        return resp
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
    INBOUND.appendleft(entry)
    log.info("webhook received (xml) path=%s bytes=%s", request.path, len(body))
    return {"message": "Request received successfully"}


@app.route("/lphc/events", methods=["POST"])  # Receives raw data (text/JSON/XML)
def webhook_incoming_raw():
    ok, resp = _require_api_key()
    if not ok:
        return resp
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
    INBOUND.appendleft(entry)
    log.info("webhook received (raw) path=%s type=%s", request.path, content_type)
    return {"message": "Request received successfully"}


@app.route("/lphc/health", methods=["GET"])  # Health check endpoint
def lphc_health():
    log.debug("health check ok")
    return {"status": "ok"}


def create_app():
    init_logging()
    log.info("webhook_service started")
    return app


if __name__ == "__main__":
    init_logging()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5042)))
