Syslog Forwarder
================

Tail `*.log` files and forward them as RFC5424 syslog (UDP/TCP/TLS). The web UI also includes simple REST and Webhook mocks so you can test end‑to‑end quickly.

What You Get
------------
- Syslog forwarder (CLI + Web UI)
- REST mock with pagination
- Webhook emitter and test receiver
- Multi‑stage Docker image
- Optional Nginx load balancer for the REST service

What’s New
----------
- Upload flow now redirects with 303 See Other (fixes 302 POST handling issues in some clients).
- UI has a consistent theme with a light/dark toggle (persists via localStorage).
- Services can run independently: UI, Syslog forwarder, REST mock, and Webhook receiver are split and runnable as separate containers via docker‑compose.

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

Environment vars (optional)
---------------------------
- `DATA_DIR`: Directory for uploads and tailed files (defaults to `/data`, then `./data`).
- `REST_BASE_URL`: If set, UI links and testers will use this base for REST endpoints (e.g., `http://rest:5041`).
- `WEBHOOK_BASE_URL`: If set, UI may display the external webhook receiver base.

Data directory
--------------
- The app stores uploads and watched files in a data directory:
  - Uses `$DATA_DIR` if set, else `/data` if present, else `./data`, else the current folder.
  - JSON uploads are saved under `data/datasets/` and only files there are treated as REST/Webhook datasets.
  - Regular files (e.g., `.log`) are saved at the root of the data directory and can be tailed by the forwarder.

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
  - Logpoint preset supported: builds `POST /lphc/events/json` with `x-api-key`.

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

Compose: Run as separate services
---------------------------------
This repo ships a `docker-compose.yml` that runs each capability as its own service while sharing the `./data` volume:

```
docker-compose up --build
```

Services and ports:
- `ui` (Web UI): http://localhost:5030
- `rest-lb` (REST load balancer): http://localhost:5041
- `rest_a` and `rest_b` (REST backends): internal only
- `webhook` (Receiver endpoint): http://localhost:5042
- `syslog` (CLI forwarder): runs the forwarder process; no port exposed

 Notes:
- The UI is configured with `REST_BASE_URL` and `WEBHOOK_BASE_URL` to point at the other services in compose. REST calls go through the `rest-lb` Nginx service which balances across `rest_a` and `rest_b`.
- All services share `./data` mounted to `/data`; upload `.json` into `/data/datasets` to appear as datasets.

REST Load Balancer
------------------
- Nginx balances across two REST backends (`rest_a`, `rest_b`).
- Exposes an indicator header `X-LB-Tag` so you can tag the LB via env:

```
LB_TAG=canary docker-compose up --build
```

You can verify the header:

```
curl -s -D - http://localhost:5041/api/rest/unknown | grep X-LB-Tag || true
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
- After upload, the UI uses HTTP 303 to redirect. If testing via curl, you may need `-L` to follow redirects.

Logpoint Webhook Preset
-----------------------
- Outbound (UI → your Logpoint receiver):
  - In Webhook tab, fill the Logpoint preset fields:
    - `Scheme`: `http` or `https`
    - `Host`: e.g., `logpoint-host`
    - `X-API-Key`: your API key
  - The UI builds `POST {scheme}://{host}/lphc/events/json` and sets `x-api-key`.
  - Optionally paste a raw JSON template (e.g., `{"hello":"world"}`) to generate dynamic payloads.
- Inbound (testing locally):
  - UI app exposes `POST /lphc/events/json` as an alias, identical to `/api/webhook/incoming`.
  - Standalone webhook service also exposes `POST /lphc/events/json`.
  - Example test call:

```
curl -sS -X POST \
  http://localhost:5030/lphc/events/json \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: YOUR_KEY' \
  -d '{"hello":"world"}'
```

Recommended Config Defaults
---------------------------
If you want defaults that match a Logpoint receiver, set the `webhook` section like this:

```
{
  "webhook": {
    "url": "http://logpoint-host/lphc/events/json",
    "method": "POST",
    "headers": { "x-api-key": "<api key>" },
    "mps": 1.0
  }
}
```

These are only defaults; the Webhook UI lets you override URL, headers, and rate at runtime and also supports the Logpoint preset builder.

UI Theme
--------
- Toggle light/dark using the “🌓 Theme” button in the header.
- Your preference is stored in the browser and applied automatically.

License
-------
For testing and demos.

Health Check / Dry Test
-----------------------
Run a quick local check (no network needed):

```
python3 health_check.py
```

What it does:
- Validates Python env and required files.
- Parses `config.json` and checks basic structure.
- Imports core modules and exercises message formatting.
- Creates Flask test clients for `webapp`, `rest_service`, and `webhook_service` and probes endpoints.
