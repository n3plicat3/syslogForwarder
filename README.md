Syslog Forwarder
================

A small Python tool that tails log files in the current folder and forwards each new line as an RFC5424 syslog message over UDP, TCP, or TLS. It also ships with a minimal Flask web UI and a JSON data simulator to help you test downstream systems quickly.

What’s Included
---------------
- CLI forwarder: `syslog_forwarder.py` (no third‑party deps).
- Web UI: `webapp.py` + `templates/` (Flask based).
- Example config: `config.json` and a sample log: `sample.log`.
- Optional JSON simulator: turn any `.json` file into a stream of synthetic messages and forward them as syslog.

Requirements
------------
- Python 3.8+
- For the web UI only: `pip install -r requirements.txt` (Flask, Werkzeug)

Quick Start (CLI)
-----------------
1) From this folder, run the forwarder using the default config:

```
python3 syslog_forwarder.py --config config.json
```

2) Write or append lines to any `*.log` file in this folder (e.g., `sample.log`). New lines are forwarded immediately.

Configuration
-------------
All settings live in `config.json` and can be overridden via CLI flags.

Top‑level keys:
- destination: `{ "host": "127.0.0.1", "port": 514, "protocol": "udp|tcp|tls" }`
- tls: `{ "ca_file": string|null, "cert_file": string|null, "key_file": string|null, "verify_mode": "required|optional|none" }`
- files: `{ "pattern": "*.log[,*.txt]", "read_from_beginning": bool, "rescan_interval_sec": number }`
- syslog: `{ "facility": "user|local0|…", "severity": "info|warn|…", "app_name": string, "host_name": string|null }`

CLI overrides
-------------
Any config value can be overridden at launch:

```
python3 syslog_forwarder.py \
  --host 192.168.1.10 --port 6514 --protocol tls \
  --cafile ca.pem --certfile client.crt --keyfile client.key --no-verify \
  --pattern "*.log,*.txt" --from-beginning \
  --facility local0 --severity info --app myapp --hostname myhost
```

Behavior Notes
--------------
- UDP sends messages directly; TCP/TLS use RFC6587 octet‑counting framing.
- TLS verification is on by default; `--no-verify` disables it (not recommended).
- The forwarder rescans the current directory every `files.rescan_interval_sec` seconds and starts tailing new files matching `files.pattern`. Multiple comma‑separated patterns are supported (e.g., `*.log,*.jsonl`).
- File rotation is detected via inode change; the tailer reopens automatically.
- Only files in the current directory are watched (no recursive subfolders).

Web UI
------
The UI lets you start/stop forwarding, upload files, edit configuration, view a live event log, and run the JSON simulator.

Setup:
- Install deps once: `python3 -m pip install -r requirements.txt`
- Start the UI: `python3 webapp.py`
- Open: `http://localhost:5030` (or `http://localhost:$PORT` if you set `PORT`)

Features:
- Start/stop the file forwarder
- Upload `.log` or `.json` files (saved to this folder)
- Edit destination/TLS/files/syslog settings (persisted to `config.json`)
- Live events via Server‑Sent Events (SSE) at `/events`

JSON Simulator
--------------
- Upload any `.json` file (object or array of objects). The UI generates a variety of realistic samples from the shape of your JSON.
- REST endpoints:
  - `GET /api/json/logs/<name>?count=10` returns an array of sample objects.
  - `GET /api/json/stream/<name>?mps=2` streams samples as SSE.
- Forward as syslog: choose a dataset and rate in the UI; messages are serialized as compact JSON strings in the syslog message body, using the destination and syslog header from `config.json`.

Project Layout
--------------
- `syslog_forwarder.py`: CLI forwarder, transports (UDP/TCP/TLS), RFC5424 formatting, tailing manager.
- `webapp.py`: Flask app, live events, config editing, JSON simulator/forwarder.
- `templates/`: HTML templates for the UI.
- `config.json`: Default configuration (loaded and shallow‑merged with user changes).
- `sample.log`: Example file you can append to for quick testing.

Environment Variables
---------------------
- `PORT`: web UI port (default: `5030`).
- `FLASK_SECRET_KEY`: set to a strong value in production.

Troubleshooting
---------------
- Nothing arrives at the destination:
  - Verify host/port/protocol and any firewall rules.
  - For TLS, check CA/cert/key paths and `verify_mode`.
  - Try `--protocol tcp` to avoid UDP drops during testing.
- No files are tailed:
  - Confirm the files are in the current directory and match `files.pattern`.
  - Use `--from-beginning` to resend existing content.
- TLS handshake fails:
  - Use the correct `--cafile` for the server, or set `verify_mode` to `none` for local testing only.

Security Considerations
-----------------------
- Treat `--no-verify` and `verify_mode: none` as test‑only. For production, validate server certificates and pin CA where possible.
- Uploaded files are saved to the working directory and immediately eligible for tailing if they match the pattern.

Limitations
-----------
- Single working directory (no recursive subfolders).
- No persistent offsets across restarts.
- Structured Data (SD) in RFC5424 is `-` and not customizable.
- UDP has no delivery guarantees or backpressure.

License
-------
This project is provided as‑is for demonstration and testing purposes.

Docker
------
Run the full application (web UI + CLI forwarder + JSON simulator) in a container. The application reads and writes files from its current working directory; the image is set up to use `/data` for that, so you can mount a host folder to persist `config.json`, uploads, and the files you want to tail.

Build the image:

```
docker build -t syslog-forwarder:latest .
```

Run the web UI:

```
mkdir -p ./data
docker run --rm -it \
  -p 5030:5030 \
  -e PORT=5030 \
  -e FLASK_SECRET_KEY=change-me \
  -v "$(pwd)/data:/data" \
  --name syslog-forwarder \
  syslog-forwarder:latest
```

Then open `http://localhost:5030`.

What goes into `./data`:
- `config.json`: configuration file (created/updated via the UI)
- `*.log`: any log files to be tailed and forwarded
- `*.json`: JSON templates for the simulator and JSON forwarder
- TLS files referenced from `config.json` (e.g., `ca.pem`, `client.crt`, `client.key`)

Forward logs from the host:
- Put or mount your logs under `./data` on the host; they appear in the container at `/data` and will be matched against `files.pattern` from `config.json` (default: `*.log`).

Use the CLI forwarder (no UI):

```
docker run --rm -it \
  -v "$(pwd)/data:/data" \
  --name forwarder-cli \
  syslog-forwarder:latest \
  python /app/syslog_forwarder.py --config config.json
```

TLS to a remote syslog server:
1. Place your CA and optional client cert/key into `./data` (e.g., `ca.pem`, `client.crt`, `client.key`).
2. In the UI (Config), set `destination.protocol` to `tls`, `tls.ca_file` to `ca.pem`, and optionally `tls.cert_file`/`tls.key_file`. Set `tls.verify_mode` as needed (recommended: `required`).

docker-compose (optional):

```
version: "3.8"
services:
  syslog-forwarder:
    build: .
    image: syslog-forwarder:latest
    container_name: syslog-forwarder
    environment:
      - PORT=5030
      - FLASK_SECRET_KEY=${FLASK_SECRET_KEY:-change-me}
    ports:
      - "5030:5030"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

Start with compose:

```
docker compose up --build
```

Override to run CLI forwarder in compose:

```
# In docker-compose.yml under the service:
command: ["python", "/app/syslog_forwarder.py", "--config", "config.json"]
```

Notes
- The container runs as a non-root user (`uid 10000`); ensure the mounted `./data` directory is writable by your user on the host.
- The web UI writes uploads and config changes to `/data`. The forwarder watches `/data` only (non-recursive).
- The image includes `gunicorn` to serve the Flask app in production mode. SSE endpoints are supported with threaded workers.
