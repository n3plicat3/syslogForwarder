#!/usr/bin/env python3
import argparse
import json
import os
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Set


# ----------------------------
# RFC5424 helpers and mappings
# ----------------------------

FACILITY_MAP = {
    "kern": 0,
    "user": 1,
    "mail": 2,
    "daemon": 3,
    "auth": 4,
    "syslog": 5,
    "lpr": 6,
    "news": 7,
    "uucp": 8,
    "cron": 9,
    "authpriv": 10,
    "ftp": 11,
    "ntp": 12,
    "security": 13,
    "console": 14,
    "solaris-cron": 15,
    "local0": 16,
    "local1": 17,
    "local2": 18,
    "local3": 19,
    "local4": 20,
    "local5": 21,
    "local6": 22,
    "local7": 23,
}

SEVERITY_MAP = {
    "emerg": 0,
    "alert": 1,
    "crit": 2,
    "err": 3,
    "error": 3,
    "warn": 4,
    "warning": 4,
    "notice": 5,
    "info": 6,
    "debug": 7,
}


def rfc3339_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def build_pri(facility: int, severity: int) -> int:
    return facility * 8 + severity


def rfc5424_message(
    msg: str,
    facility: int,
    severity: int,
    host_name: Optional[str],
    app_name: str,
    procid: str,
) -> str:
    pri = build_pri(facility, severity)
    timestamp = rfc3339_now()
    hostname = host_name or socket.gethostname()
    version = 1
    msgid = "-"
    structured_data = "-"
    # RFC5424 format: <PRI>VERSION TIMESTAMP HOST APP PROCID MSGID [SD] MSG
    return f"<{pri}>{version} {timestamp} {hostname} {app_name} {procid} {msgid} {structured_data} {msg}"


# ----------------------------
# Transport senders
# ----------------------------

class SyslogSender:
    def send(self, message: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class UdpSender(SyslogSender):
    def __init__(self, host: str, port: int):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lock = threading.Lock()

    def send(self, message: str) -> None:
        data = message.encode("utf-8", errors="replace")
        with self.lock:
            self.sock.sendto(data, self.addr)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class TcpSender(SyslogSender):
    def __init__(self, host: str, port: int, ssl_context: Optional[ssl.SSLContext] = None):
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.sock = None  # type: Optional[socket.socket]
        self.lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=10)
        if self.ssl_context is not None:
            s = self.ssl_context.wrap_socket(s, server_hostname=self.host)
        self.sock = s

    def _ensure_connected(self):
        if self.sock is None:
            self._connect()

    def send(self, message: str) -> None:
        data = message.encode("utf-8", errors="replace")
        # RFC6587 octet-counting framing for TCP (and TLS)
        framed = f"{len(data)} ".encode("ascii") + data
        with self.lock:
            try:
                self._ensure_connected()
                assert self.sock is not None
                self.sock.sendall(framed)
            except Exception:
                # Attempt one reconnect per failure path
                try:
                    if self.sock is not None:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                    self.sock = None
                    self._connect()
                    assert self.sock is not None
                    self.sock.sendall(framed)
                except Exception:
                    # Drop message on persistent failure
                    pass

    def close(self) -> None:
        with self.lock:
            try:
                if self.sock is not None:
                    self.sock.close()
            except Exception:
                pass
            finally:
                self.sock = None


def build_sender(cfg: Dict) -> SyslogSender:
    dest = cfg.get("destination", {})
    host = dest.get("host", "127.0.0.1")
    port = int(dest.get("port", 514))
    protocol = dest.get("protocol", "udp").lower()

    if protocol == "udp":
        return UdpSender(host, port)
    elif protocol in ("tcp", "tls"):
        ctx = None
        if protocol == "tls":
            tls_cfg = cfg.get("tls", {})
            ctx = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH,
                cafile=tls_cfg.get("ca_file") or None,
            )
            verify_mode = (tls_cfg.get("verify_mode") or "required").lower()
            if verify_mode in ("none", "optional"):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            else:
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
            cert_file = tls_cfg.get("cert_file")
            key_file = tls_cfg.get("key_file")
            if cert_file:
                ctx.load_cert_chain(certfile=cert_file, keyfile=key_file or None)
        return TcpSender(host, port, ctx)
    else:
        raise SystemExit(f"Unsupported protocol: {protocol}")


# ----------------------------
# File tailing
# ----------------------------

class FileTailer(threading.Thread):
    def __init__(
        self,
        path: Path,
        sender: SyslogSender,
        facility: int,
        severity: int,
        app_name: str,
        host_name: Optional[str],
        read_from_beginning: bool = False,
        poll_interval: float = 0.25,
        on_event: Optional[callable] = None,
    ):
        super().__init__(daemon=True)
        self.path = path
        self.sender = sender
        self.facility = facility
        self.severity = severity
        self.app_name = app_name
        self.host_name = host_name
        self.read_from_beginning = read_from_beginning
        self.poll_interval = poll_interval
        self.on_event = on_event
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _open_file(self):
        f = self.path.open("r", encoding="utf-8", errors="replace")
        if not self.read_from_beginning:
            # Seek to end for new-only behavior
            f.seek(0, os.SEEK_END)
        return f

    def run(self):
        try:
            f = self._open_file()
        except FileNotFoundError:
            # If file disappears, just exit thread
            return
        procid = str(os.getpid())
        last_inode = None
        try:
            last_inode = os.fstat(f.fileno()).st_ino
        except Exception:
            pass
        while not self._stop.is_set():
            line = f.readline()
            if not line:
                # Detect rotation/truncation
                try:
                    st = os.stat(self.path)
                    if last_inode is not None and st.st_ino != last_inode:
                        f.close()
                        f = self._open_file()
                        last_inode = os.fstat(f.fileno()).st_ino
                except FileNotFoundError:
                    # Wait for file to reappear
                    time.sleep(self.poll_interval)
                time.sleep(self.poll_interval)
                continue
            msg = line.rstrip("\r\n")
            syslog_msg = rfc5424_message(
                msg=msg,
                facility=self.facility,
                severity=self.severity,
                host_name=self.host_name,
                app_name=self.app_name,
                procid=procid,
            )
            try:
                self.sender.send(syslog_msg)
                if self.on_event is not None:
                    try:
                        self.on_event(
                            {
                                "type": "line",
                                "file": str(self.path),
                                "app": self.app_name,
                                "facility": self.facility,
                                "severity": self.severity,
                                "timestamp": rfc3339_now(),
                                "message": msg,
                            }
                        )
                    except Exception:
                        pass
            except Exception:
                # Drop line on failure
                pass
        try:
            f.close()
        except Exception:
            pass


class TailManager:
    def __init__(
        self,
        sender: SyslogSender,
        pattern: str,
        facility: int,
        severity: int,
        app_name: str,
        host_name: Optional[str],
        read_from_beginning: bool,
        rescan_interval: float,
        directory: Path,
        on_event: Optional[callable] = None,
    ):
        self.sender = sender
        self.pattern = pattern
        self.facility = facility
        self.severity = severity
        self.app_name = app_name
        self.host_name = host_name
        self.read_from_beginning = read_from_beginning
        self.rescan_interval = rescan_interval
        self.directory = directory
        self.tailers: Dict[Path, FileTailer] = {}
        self.on_event = on_event
        self._stop = threading.Event()

    def _scan(self) -> Set[Path]:
        # Support comma-separated patterns for "various log files"
        patterns = [p.strip() for p in str(self.pattern).split(",") if p.strip()]
        if not patterns:
            patterns = ["*.log"]
        found: Set[Path] = set()
        for pat in patterns:
            for p in self.directory.glob(pat):
                found.add(p)
        return found

    def start(self):
        # Start existing files
        for p in sorted(self._scan()):
            self._start_tailer(p)
        # Rescan loop
        try:
            while not self._stop.is_set():
                current = self._scan()
                # Start new files
                for p in current:
                    if p not in self.tailers:
                        self._start_tailer(p)
                # Stop removed files
                for p in list(self.tailers.keys()):
                    if p not in current:
                        self._stop_tailer(p)
                time.sleep(self.rescan_interval)
        finally:
            for p in list(self.tailers.keys()):
                self._stop_tailer(p)
            self.sender.close()

    def stop(self):
        self._stop.set()

    def _start_tailer(self, path: Path):
        t = FileTailer(
            path=path,
            sender=self.sender,
            facility=self.facility,
            severity=self.severity,
            app_name=self.app_name,
            host_name=self.host_name,
            read_from_beginning=self.read_from_beginning,
            on_event=self.on_event,
        )
        t.start()
        self.tailers[path] = t
        if self.on_event is not None:
            try:
                self.on_event(
                    {
                        "type": "start",
                        "file": str(path),
                        "timestamp": rfc3339_now(),
                    }
                )
            except Exception:
                pass

    def _stop_tailer(self, path: Path):
        t = self.tailers.pop(path, None)
        if t is not None:
            t.stop()
        if self.on_event is not None:
            try:
                self.on_event(
                    {
                        "type": "stop",
                        "file": str(path),
                        "timestamp": rfc3339_now(),
                    }
                )
            except Exception:
                pass


# ----------------------------
# Config + CLI
# ----------------------------

DEFAULT_CONFIG = {
    "destination": {"host": "127.0.0.1", "port": 514, "protocol": "udp"},
    "tls": {
        "enabled": False,
        "ca_file": None,
        "cert_file": None,
        "key_file": None,
        "verify_mode": "required",
    },
    "files": {
        "pattern": "*.log",
        "read_from_beginning": False,
        "rescan_interval_sec": 5,
    },
    "syslog": {
        "facility": "user",
        "app_name": "forwarder",
        "host_name": None,
        "severity": "info",
    },
}


def load_config(path: Optional[str]) -> Dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        # shallow merge by top-level sections
        for k, v in user_cfg.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def parse_args():
    p = argparse.ArgumentParser(description="Simple *.log syslog forwarder")
    p.add_argument("--config", help="Path to config JSON", default="config.json")
    p.add_argument("--host", help="Destination host override")
    p.add_argument("--port", type=int, help="Destination port override")
    p.add_argument("--protocol", choices=["udp", "tcp", "tls"], help="Transport protocol")
    p.add_argument("--pattern", help="Glob pattern of files to follow (default: *.log)")
    p.add_argument("--from-beginning", action="store_true", help="Read files from beginning")
    p.add_argument("--cafile", help="TLS CA certificate file")
    p.add_argument("--certfile", help="TLS client certificate file")
    p.add_argument("--keyfile", help="TLS client key file")
    p.add_argument("--no-verify", action="store_true", help="Disable TLS cert verification")
    p.add_argument("--facility", help="Syslog facility (e.g. user, local0)")
    p.add_argument("--severity", help="Default severity (e.g. info, warn)")
    p.add_argument("--app", help="App name for syslog header")
    p.add_argument("--hostname", help="Override hostname in syslog header")
    return p.parse_args()


def apply_overrides(cfg: Dict, args) -> Dict:
    # Destination
    if args.host:
        cfg.setdefault("destination", {})["host"] = args.host
    if args.port:
        cfg.setdefault("destination", {})["port"] = args.port
    if args.protocol:
        cfg.setdefault("destination", {})["protocol"] = args.protocol

    # Files
    if args.pattern:
        cfg.setdefault("files", {})["pattern"] = args.pattern
    if args.from_beginning:
        cfg.setdefault("files", {})["read_from_beginning"] = True

    # TLS
    if args.cafile or args.certfile or args.keyfile or args.no_verify:
        tls = cfg.setdefault("tls", {})
        if args.cafile:
            tls["ca_file"] = args.cafile
        if args.certfile:
            tls["cert_file"] = args.certfile
        if args.keyfile:
            tls["key_file"] = args.keyfile
        if args.no_verify:
            tls["verify_mode"] = "none"
        # If protocol not set but TLS opts present, assume tls
        dest = cfg.setdefault("destination", {})
        if dest.get("protocol", "udp").lower() != "tls":
            dest["protocol"] = "tls"

    # Syslog header
    if args.facility:
        cfg.setdefault("syslog", {})["facility"] = args.facility
    if args.severity:
        cfg.setdefault("syslog", {})["severity"] = args.severity
    if args.app:
        cfg.setdefault("syslog", {})["app_name"] = args.app
    if args.hostname:
        cfg.setdefault("syslog", {})["host_name"] = args.hostname

    return cfg


def main():
    args = parse_args()
    cfg_path = args.config if args.config and os.path.exists(args.config) else None
    cfg = load_config(cfg_path)
    cfg = apply_overrides(cfg, args)

    # Resolve syslog params
    syslog_cfg = cfg.get("syslog", {})
    facility_name = (syslog_cfg.get("facility") or "user").lower()
    severity_name = (syslog_cfg.get("severity") or "info").lower()
    facility = FACILITY_MAP.get(facility_name)
    severity = SEVERITY_MAP.get(severity_name)
    if facility is None:
        raise SystemExit(f"Unknown facility: {facility_name}")
    if severity is None:
        raise SystemExit(f"Unknown severity: {severity_name}")
    app_name = syslog_cfg.get("app_name") or "forwarder"
    host_name = syslog_cfg.get("host_name") or None

    # Files
    files_cfg = cfg.get("files", {})
    pattern = files_cfg.get("pattern", "*.log")
    read_from_beginning = bool(files_cfg.get("read_from_beginning", False))
    rescan_interval = float(files_cfg.get("rescan_interval_sec", 5))

    # Sender
    sender = build_sender(cfg)

    # Tail manager
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
    )

    try:
        manager.start()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
