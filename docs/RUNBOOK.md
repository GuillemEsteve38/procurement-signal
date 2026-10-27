# Runbook

One image, two kinds of process: the web UI (long-running) and the ingest and
enrich jobs (one-shot). They share one SQLite file on the `data` volume. The web
process opens it read-only, so a job can write while the UI is serving.

## Configuration

| Variable | Default (image) | Default (local) | Used by |
|---|---|---|---|
| `PROCSIGNAL_DB` | `/data/procurement.sqlite3` | `data/procurement.sqlite3` | every CLI and the web app |
| `PROCSIGNAL_HOST` | `0.0.0.0` | `127.0.0.1` | web |
| `PORT` | `8000` | `8000` | web (the name PaaS hosts set) |
| `ANTHROPIC_API_KEY` | unset | unset | enrich / evaluate with `--extractor claude` only |

Command-line flags override the environment. The TED API needs no key.

## First deploy

```sh
docker compose build
docker compose run --rm ingest --days 120      # backfill, ~25s for 2.7k notices
docker compose up -d web
curl -s localhost:8000/healthz
```

Ingest before starting `web`: with no database the web process exits with
`no database at ...` and compose restarts it until one exists.

## Daily run

Freshness target is "visible within 24h of TED publication". Run a small
overlapping window once a day; ingest is idempotent, so the overlap costs
nothing and absorbs a missed day.

```sh
docker compose run --rm ingest --days 3
```

Schedule that with whatever the host has: cron (`15 6 * * *`) on Linux, Task
Scheduler on Windows. If the host was down for longer than the window, rerun
with `--since YYYY-MM-DD` from the last good day (`inspect_db.py runs` shows it).

## Checking it is healthy

`GET /healthz` returns 200 with `notices`, `latest_publication`,
`last_ingest_at` and `schema_version`, or 503 if the database is unreadable.
The container's `HEALTHCHECK` probes it.

It reports 200 even when the data is stale. Alert on `last_ingest_at` older
than ~26h, not just on the status code.

## When something breaks

| Symptom | Likely cause | What to do |
|---|---|---|
| Ingest exits non-zero with `TedQueryError` | Our query is wrong, or TED changed a field name | Read the structured error (field + line/column). Do not retry; fix the profile or field list |
| Ingest logs repeated 429/5xx then fails | TED rate limiting or outage | Rerun later. The client already backs off; rerunning the same window is safe |
| `timedOut: true` error | Window returned too much for one scroll | Rerun with a smaller `--window-days` |
| A run row stuck at `running` | Job was killed mid-run | Rerun the same window. No duplicates, no gaps |
| `inspect_db.py anomalies` shows new API fields | TED schema drift | Raw payloads are kept; extend `normalize.py`, then re-ingest the window to rewrite rows |
| `/healthz` 503 | Volume not mounted or file corrupt | `docker compose run --rm --entrypoint sh web -c "ls -l /data"` |

## Backup

The database is the only state. SQLite's online backup is safe while jobs run:

```sh
docker compose run --rm --entrypoint python web -c "import sqlite3; s=sqlite3.connect('/data/procurement.sqlite3'); s.backup(sqlite3.connect('/data/backup.sqlite3'))"
docker compose cp web:/data/backup.sqlite3 ./backup.sqlite3
```

The second line matters: the first one writes into the same volume as the
live database, so without copying it out, deleting the volume (`docker compose
down -v`) takes the backup with it. `cp` needs the `web` container running.

Losing it costs a re-crawl (~25s per 120 days of this profile), plus any
Claude extractions, which cost money to recreate. Back up before an enrich run.

## Running without Docker

```sh
.venv/Scripts/python.exe -m pip install -e ".[web]"
.venv/Scripts/python.exe -m procsignal.ingest --days 3
.venv/Scripts/python.exe -m procsignal.web
```
