#!/usr/bin/env python3
"""
Health and dry-test script for Syslog Forwarder.

Runs quick, local checks without external network access:
- Validates Python env and required files/dirs
- Validates config.json structure and fields
- Imports key modules and constructs core objects
- Spins up Flask app factories in test mode and probes a few endpoints

Exit code is non-zero on failures to aid CI.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def err(msg: str):
    print(f"[ERROR] {msg}")


def ok(msg: str):
    print(f"[ OK ] {msg}")


def warn(msg: str):
    print(f"[WARN] {msg}")


def check_python():
    major, minor = sys.version_info[:2]
    if major < 3 or (major == 3 and minor < 10):
        err(f"Python 3.10+ recommended, found {major}.{minor}")
        return False
    ok(f"Python {major}.{minor} detected")
    return True


def find_data_dir() -> Path:
    candidates = []
    env_dir = os.environ.get("DATA_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend([Path("/data"), ROOT / "data", ROOT])
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return ROOT


def check_files():
    required = [
        ROOT / "webapp.py",
        ROOT / "syslog_forwarder.py",
        ROOT / "rest_service.py",
        ROOT / "webhook_service.py",
        ROOT / "templates",
    ]
    ok_all = True
    for p in required:
        if not p.exists():
            err(f"Missing: {p.relative_to(ROOT)}")
            ok_all = False
    if ok_all:
        ok("Core files present")
    return ok_all


def load_config(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        warn("config.json not found — defaults will apply in app")
        return {}
    except Exception as e:
        err(f"Failed to parse config.json: {e}")
        return {}


def validate_config(cfg: dict) -> bool:
    ok_all = True
    dest = cfg.get("destination", {})
    if dest and not isinstance(dest, dict):
        err("destination must be an object")
        ok_all = False
    files = cfg.get("files", {})
    if files and not isinstance(files, dict):
        err("files must be an object")
        ok_all = False
    syslog = cfg.get("syslog", {})
    if syslog and not isinstance(syslog, dict):
        err("syslog must be an object")
        ok_all = False
    tls = cfg.get("tls", {})
    if tls and not isinstance(tls, dict):
        err("tls must be an object")
        ok_all = False
    if ok_all:
        ok("Config structure looks valid")
    return ok_all


def check_imports_and_core():
    try:
        import syslog_forwarder as sf  # noqa: F401
        from syslog_forwarder import (
            rfc5424_message,
            build_pri,
            FACILITY_MAP,
            SEVERITY_MAP,
        )
    except Exception as e:
        err(f"Import syslog_forwarder failed: {e}")
        return False

    # basic message formatting
    try:
        pri = build_pri(FACILITY_MAP["user"], SEVERITY_MAP["info"])
        msg = rfc5424_message(
            msg="test",
            facility=FACILITY_MAP["user"],
            severity=SEVERITY_MAP["info"],
            host_name=None,
            app_name="health",
            procid="0",
        )
        assert isinstance(pri, int) and msg.startswith("<") and " health " in msg
    except Exception as e:
        err(f"RFC5424 helpers failed: {e}")
        return False
    ok("Core helpers operational")
    return True


def check_flask_apps():
    """Spin up Flask apps in test mode and probe a few endpoints."""
    ok_all = True
    try:
        import rest_service as rs
        app = rs.create_app()
        client = app.test_client()
        rv = client.get("/api/rest/UNKNOWN")
        assert rv.status_code in (200, 404)
        ok("REST app factory responds")
    except Exception as e:
        err(f"REST app probe failed: {e}")
        ok_all = False

    try:
        import webhook_service as wh
        app = wh.create_app()
        client = app.test_client()
        rv = client.get("/status")
        assert rv.status_code == 200
        ok("Webhook app factory responds")
    except Exception as e:
        err(f"Webhook app probe failed: {e}")
        ok_all = False

    try:
        import webapp as ui
        app = ui.app  # global app instance
        client = app.test_client()
        rv = client.get("/")
        assert rv.status_code == 200
        ok("UI app responds")
    except Exception as e:
        err(f"UI app probe failed: {e}")
        ok_all = False

    return ok_all


def check_datasets(data_dir: Path):
    ds = data_dir / "datasets"
    ds.mkdir(parents=True, exist_ok=True)
    files = list(ds.glob("*.json"))
    if not files:
        warn("No datasets (*.json) in data/datasets — REST/Webhook demos will be empty")
    else:
        ok(f"Found {len(files)} dataset(s) in data/datasets")
    return True


def main() -> int:
    status = True
    status &= check_python()
    status &= check_files()

    cfg = load_config(ROOT / "config.json")
    status &= validate_config(cfg)

    data_dir = find_data_dir()
    ok(f"Data dir: {data_dir}")
    status &= check_datasets(data_dir)

    status &= check_imports_and_core()
    status &= check_flask_apps()

    print("\nSummary:")
    if status:
        ok("Health check passed")
        return 0
    else:
        err("Health check failed — see messages above")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

