Simple Syslog Forwarder
=======================

This is a minimal Python tool that tails all `*.log` files in the current folder and forwards new lines as RFC5424 syslog messages to a specified destination over UDP, TCP, or TLS.

Quick Start
-----------

- Ensure Python 3.8+ is installed.
- From this folder, run:

```
python3 syslog_forwarder.py --config config.json
```

Config
------

Edit `config.json` to change destination, TLS, file behavior, and syslog header fields.

Key options:
- destination.host / destination.port / destination.protocol ("udp" | "tcp" | "tls")
- tls.ca_file, tls.cert_file, tls.key_file, tls.verify_mode ("required" | "none")
- files.pattern (glob, default `*.log`), files.read_from_beginning, files.rescan_interval_sec
- syslog.facility (e.g. user, local0), syslog.severity (e.g. info), syslog.app_name, syslog.host_name

CLI Overrides
-------------

Any of these can be overridden on the command line:

```
python3 syslog_forwarder.py \
  --host 192.168.1.10 --port 6514 --protocol tls \
  --cafile ca.pem --certfile client.crt --keyfile client.key \
  --pattern "*.log" --from-beginning \
  --facility local0 --severity info --app myapp --hostname myhost
```

Notes
-----
- UDP sends messages directly; TCP/TLS uses RFC6587 octet-counting framing.
- TLS verification is enabled by default; pass `--no-verify` to disable (not recommended).
- The tool rescans the folder periodically and picks up new `*.log` files.
- File rotations are handled simply by inode change detection.

Web UI (Flask)
---------------

A simple web UI is available to start/stop forwarding, upload files, edit configuration, and view live transfer logs.

- Install dependencies (one-time):

```
python3 -m pip install -r requirements.txt
```

- Run the UI:

```
python3 webapp.py
```

- Open http://localhost:5000 in your browser.

Features:
- Start/stop the forwarder
- Upload `.log` files (saved to the current directory)
- Edit destination/syslog/file settings (saved to `config.json`)
- Live "sent line" stream via Server-Sent Events (SSE)
