# One image for both processes: the web UI (default CMD) and the ingest/enrich
# jobs (override the command). They share the database through the /data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PROCSIGNAL_DB=/data/procurement.sqlite3 \
    PROCSIGNAL_HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[web,extract]"

RUN useradd --create-home --uid 1000 app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

CMD ["python", "-m", "procsignal.web"]
