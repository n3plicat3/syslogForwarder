#!/usr/bin/env python3
import os
import time
from collections import deque
from typing import Deque, Dict

from flask import Flask, request

app = Flask(__name__)

INBOUND: Deque[Dict] = deque(maxlen=500)


@app.route("/api/webhook/incoming", methods=["POST", "PUT"])
def webhook_incoming():
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
    return {"status": "ok"}


@app.route("/status")
def status():
    return {"inbound_count": len(INBOUND)}


def create_app():
    return app


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5042)))

