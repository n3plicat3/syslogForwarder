# syntax=docker/dockerfile:1

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5030 \
    PYTHONPATH=/app

# System deps (none strictly required; keep image slim)
RUN groupadd -g 10001 app && useradd -m -u 10000 -g app app

WORKDIR /app

# Copy requirement files first for better layer caching
COPY requirements.txt /app/requirements.txt

# Install runtime deps and a WSGI server
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application source
COPY webapp.py syslog_forwarder.py config.json /app/
COPY templates /app/templates

# Working directory for data, uploads, config and tailed logs
RUN mkdir -p /data && chown -R app:app /data
WORKDIR /data

USER app

# Expose the web UI port
EXPOSE 5030

#
# Default: run the web UI with gunicorn, while using /data as CWD so
# uploads, config.json and tailed files live on the mounted volume.
#
# The Flask app factory is webapp:create_app()
CMD [ \
  "gunicorn", \
  "--workers", "2", \
  "--threads", "4", \
  "--bind", "0.0.0.0:5030", \
  "--timeout", "0", \
  "webapp:create_app" \
]

#
# To run the CLI forwarder instead of the UI, override the command, e.g.:
#   docker run --rm -it -v "$PWD/data:/data" --name forwarder \
#     syslog-forwarder:latest \
#     python /app/syslog_forwarder.py --config config.json
