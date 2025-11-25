Syslog Forwarder
================

Tail `*.log` files and forward them as RFC5424 syslog (UDP/TCP/TLS). The web UI also includes simple REST and Webhook mocks so you can test end‑to‑end quickly.

What You Get
------------
- Syslog forwarder (CLI + Web UI)
- REST mock with pagination
- Webhook emitter and test receiver
- Multi‑stage Docker image

Run the Web UI
--------------
1) Install deps

```
python3 -m pip install -r requirements.txt
```

2) Start

```
python3 webapp.py
```

3) Open `http://localhost:5030`

Use The UI
----------
- Syslog
  - Upload `.log` files and press Start to forward new lines.
  - Live Log shows tail, JSON, and webhook events.
  - Edit destination/TLS/syslog under Config.
- REST
  - Upload a `.json` file to create a dataset.
  - Paginated endpoint: `GET /api/rest/<dataset>?page=1&per_page=20`
  - Also available: `GET /api/json/logs/<dataset>?count=10`, `GET /api/json/stream/<dataset>?mps=2`.
- Webhook
  - Configure URL, method, headers, and messages/second.
  - Pick a dataset and Start to emit real HTTP requests.
  - Optional local receiver: `POST /api/webhook/incoming`

CLI (no UI)
-----------
```
python3 syslog_forwarder.py --config config.json
```

Docker
------
Build and run the UI (multi‑stage build; data lives in `/data`):

```
docker build -t syslog-forwarder:latest .
docker run --rm -it \
  -p 5030:5030 \
  -v "$PWD/data:/data" \
  --name syslog-forwarder \
  syslog-forwarder:latest
```

Run the CLI in the container:

```
docker run --rm -it \
  -v "$PWD/data:/data" \
  syslog-forwarder:latest \
  python /app/syslog_forwarder.py --config config.json
```

Minimal Config
--------------
- `destination`: `{ host, port, protocol: udp|tcp|tls }`
- `syslog`: `{ facility, severity, app_name, host_name? }`
- `files`: `{ pattern: "*.log[,*.txt]", read_from_beginning, rescan_interval_sec }`
- `tls`: `{ ca_file?, cert_file?, key_file?, verify_mode: required|optional|none }`
- `webhook` (optional defaults): `{ url?, method?, headers?, mps? }`

Tips
----
- Prefer TCP/TLS during testing (UDP can drop).
- Multiple patterns work: `"*.log,*.jsonl"`.
- Uploading a `.json` file creates a dataset you can page through at `/api/rest/<dataset>`.

License
-------
For testing and demos.

