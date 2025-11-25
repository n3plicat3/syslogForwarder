#!/usr/bin/env python3
import json
import os
import random
import time
from pathlib import Path
from typing import Dict

from flask import Flask, Response, request, url_for

app = Flask(__name__)

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
    get_data_dir()
    return app


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5041)))

