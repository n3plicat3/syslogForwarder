#!/usr/bin/env python3
import json
import os
import random
import time
import logging
import sys
from pathlib import Path
from typing import Dict

from flask import Flask, Response, request, url_for, g

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


log = logging.getLogger("rest_service")


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

DATA_DIR: Path | None = None
JSON_TEMPLATES: Dict[str, dict] = {}
JSON_SAMPLES: Dict[str, list] = {}


def get_data_dir() -> Path:
    global DATA_DIR
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
    (DATA_DIR / "datasets").mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _rand_like(value):
    import random as _r
    if isinstance(value, bool):
        return bool(_r.getrandbits(1))
    if isinstance(value, int):
        base = value; jitter = max(1, abs(base) // 10)
        return base + _r.randint(-jitter, jitter)
    if isinstance(value, float):
        base = value; jitter = abs(base) * 0.1 if base != 0 else 1.0
        return round(base + _r.uniform(-jitter, jitter), 6)
    if isinstance(value, str):
        if len(value) >= 10 and any(ch.isdigit() for ch in value):
            now = int(time.time()); return str(now - _r.randint(0, 3600))
        prefix = value[: max(0, min(len(value), 6))]
        return f"{prefix}{_r.randint(1000, 9999)}"
    if isinstance(value, dict):
        return {k: _rand_like(v) for k, v in value.items()}
    if isinstance(value, list):
        n = len(value); n2 = max(0, min(n + random.randint(-1, 2), n + 2))
        base = value or ["item"]
        return [_rand_like(random.choice(base)) for _ in range(n2)]
    return value


def generate_samples_from_template(template: dict, count: int = 500) -> list:
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
    ddir = get_data_dir()
    ds_dir = ddir / "datasets"
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


@app.route("/api/json/logs/<name>")
def api_json_logs(name: str):
    ensure_json_loaded()
    count = int(request.args.get("count", 10))
    name = Path(name).stem
    if name not in JSON_SAMPLES:
        return {"error": "unknown dataset"}, 404
    samples = JSON_SAMPLES[name]
    start = random.randint(0, max(0, len(samples) - 1))
    out = [samples[(start + i) % len(samples)] for i in range(max(1, min(count, 1000)))]
    return Response(json.dumps(out), mimetype="application/json")


@app.route("/api/json/stream/<name>")
def api_json_stream(name: str):
    ensure_json_loaded()
    name = Path(name).stem
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


def _random_ipv4(mode: str = "any") -> str:
    """Generate a structurally valid IPv4 string.
    mode: 'private' chooses RFC1918 ranges; 'public' avoids common private ranges; 'any' mixes.
    """
    m = (mode or "any").lower()
    r = random.randint
    if m == "private":
        choice = random.choice([
            (10, r(0, 255), r(0, 255), r(1, 254)),
            (172, r(16, 31), r(0, 255), r(1, 254)),
            (192, 168, r(0, 255), r(1, 254)),
        ])
    elif m == "public":
        # Use TEST-NET-2/3 and some typical public prefixes to stay deterministic and harmless
        choice = random.choice([
            (198, 51, 100, r(1, 254)),
            (203, 0, 113, r(1, 254)),
            (8, 8, r(0, 255), r(1, 254)),
            (1, 1, r(0, 255), r(1, 254)),
        ])
    else:
        # any
        choice = (r(1, 223), r(0, 255), r(0, 255), r(1, 254))
    return ".".join(str(x) for x in choice)


def _mutate_entry_preserving_schema(entry: dict, ip_mode: str = "any") -> dict:
    """Return a mutated copy of a log entry dict.
    - Randomize IP-like fields: src, dst, natsrc, natdst
    - Randomize ports if present
    - Jitter timestamps/numbers via _rand_like for variety
    """
    def mutate(k, v):
        lk = str(k).lower()
        if isinstance(v, str) and lk in ("src", "dst", "natsrc", "natdst"):
            return _random_ipv4(ip_mode)
        if lk in ("srcport", "dstport", "nat_srcport", "nat_dstport"):
            return str(random.randint(1024, 65535))
        # Light tweak for known time-ish fields: keep structure as string
        if isinstance(v, str) and any(t in lk for t in ("time", "start", "receive")):
            try:
                now = int(time.time())
                return str(now - random.randint(0, 3600))
            except Exception:
                return _rand_like(v)
        # Default: similar randomized value
        return _rand_like(v)

    out = {}
    for k, v in entry.items():
        if isinstance(v, dict):
            out[k] = {kk: mutate(kk, vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            out[k] = [mutate(k, vv) for vv in v]
        else:
            out[k] = mutate(k, v)
    return out


@app.route("/api/rest/expand/<name>")
def api_rest_expand_nested(name: str):
    """Expand nested result.logs.entry array for a dataset while preserving schema.
    Query params:
      - n / extra: number of additional synthetic entries to append (default 0)
      - total: desired final length of result.logs.entry (if larger than current)
      - seed: optional reproducibility seed
      - ip_mode: any|private|public for IP generation
    Returns the fully-hydrated JSON payload with result.count synchronized.
    """
    # Load raw dataset file directly to preserve full schema
    ddir = get_data_dir() / "datasets"
    src = ddir / f"{Path(name).stem}.json"
    if not src.exists():
        return {"error": "unknown dataset"}, 404
    try:
        with open(src, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {"error": "failed to read dataset"}, 500

    # Optional seed for reproducibility
    seed_raw = request.args.get("seed")
    if seed_raw not in (None, ""):
        try:
            random.seed(int(seed_raw))
        except Exception:
            pass

    # Determine how many to add
    def _to_int(name_, default):
        try:
            return int(request.args.get(name_, default))
        except Exception:
            return default
    extra = _to_int("n", None)
    if extra is None:
        extra = _to_int("extra", 0)
    total = request.args.get("total")
    desired_total = None
    if total not in (None, ""):
        try:
            desired_total = max(0, int(total))
        except Exception:
            desired_total = None

    ip_mode = (request.args.get("ip_mode") or "any").lower()

    # Navigate to result.logs.entry and ensure it's a list
    try:
        result = payload.get("result") if isinstance(payload, dict) else None
        logs = result.get("logs") if isinstance(result, dict) else None
        entries = logs.get("entry") if isinstance(logs, dict) else None
        if not isinstance(entries, list):
            return {"error": "payload does not contain result.logs.entry list"}, 400
    except Exception:
        return {"error": "invalid payload structure"}, 400

    current_len = len(entries)
    # Reconcile desired_total vs extra
    if desired_total is not None and desired_total > current_len:
        to_add = desired_total - current_len
    else:
        to_add = max(0, int(extra or 0))

    # Pick a base entry shape (prefer first)
    base_entry = entries[0] if entries else {}
    # Generate and append
    for _ in range(to_add):
        new_e = _mutate_entry_preserving_schema(base_entry, ip_mode=ip_mode)
        entries.append(new_e)

    # Sync count to actual length
    try:
        if isinstance(result, dict):
            result["count"] = len(entries)
    except Exception:
        pass

    return Response(json.dumps(payload), mimetype="application/json")


@app.route("/api/rest/<name>")
def api_rest_paginated(name: str):
    ensure_json_loaded()
    name = Path(name).stem
    if name not in JSON_SAMPLES:
        return {"error": "unknown dataset"}, 404
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


def create_app():
    init_logging()
    get_data_dir()
    log.info("rest_service started")
    return app


if __name__ == "__main__":
    init_logging()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5041)))
