"""FastAPI + HTMX search UI.

    python -m procsignal.web            # http://127.0.0.1:8000

Two things worth knowing about the design:

* **The database is opened read-only.** The web process cannot write, so no
  amount of mistakes in here can damage an ingest.
* **The keyword-relevance flag is shown, not hidden.** Every row renders the
  keywords that caused it to be tagged relevant, because the filter has known
  false positives — a EUR 58.7m road-pavement contract matches on "salud".
  Showing the reason turns a silent wrong answer into a visible one.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .queries import CPV_DIVISION_LABELS, SearchFilters, SearchRepo

LOG = logging.getLogger("procsignal.web")

HERE = Path(__file__).resolve().parent
DEFAULT_DB = os.environ.get("PROCSIGNAL_DB", "data/procurement.sqlite3")


def create_app(db_path: str | Path = DEFAULT_DB) -> FastAPI:
    app = FastAPI(
        title="Procurement Signal",
        description="Search EU procurement notices for a target market profile.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    repo = SearchRepo(db_path)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["eur"] = _fmt_eur
    templates.env.filters["day"] = _fmt_day
    templates.env.filters["deadline_class"] = _deadline_class
    templates.env.globals["cpv_label"] = lambda d: CPV_DIVISION_LABELS.get(
        d or "", d or "—"
    )

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness plus data freshness, for a deployment to probe."""
        try:
            payload = repo.health()
        except Exception as exc:  # noqa: BLE001 - must answer, not raise
            return JSONResponse(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                status_code=503,
            )
        return JSONResponse(payload)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        filters = SearchFilters.from_query_params(request.query_params)
        rows, total = repo.search(filters)
        facets = repo.facets(filters)
        context = _context(request, filters, rows, total, facets)

        # HTMX swaps only the results region; a full page load renders the
        # whole shell. Same handler, same filters, two templates.
        template = (
            "_results.html"
            if request.headers.get("hx-request") and not request.headers.get("hx-boosted")
            else "index.html"
        )
        return templates.TemplateResponse(request, template, context)

    @app.get("/notice/{publication_number}", response_class=HTMLResponse)
    def notice(request: Request, publication_number: str) -> Response:
        record = repo.notice(publication_number)
        if record is None:
            return templates.TemplateResponse(
                request,
                "not_found.html",
                {"publication_number": publication_number},
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "notice.html",
            {"n": record, "today": dt.date.today().isoformat()},
        )

    @app.get("/api/search")
    def api_search(request: Request) -> JSONResponse:
        """Same filters, JSON out — so the UI is not the only way in."""
        filters = SearchFilters.from_query_params(request.query_params)
        rows, total = repo.search(filters)
        return JSONResponse(
            {
                "total": total,
                "page": filters.page,
                "per_page": filters.per_page,
                "results": rows,
            }
        )

    return app


def _context(
    request: Request,
    filters: SearchFilters,
    rows: list[dict[str, Any]],
    total: int,
    facets: dict[str, Any],
) -> dict[str, Any]:
    pages = max(1, -(-total // filters.per_page))  # ceiling division
    return {
        "filters": filters,
        "rows": rows,
        "total": total,
        "facets": facets,
        "page": filters.page,
        "pages": pages,
        "has_prev": filters.page > 1,
        "has_next": filters.page < pages,
        "today": dt.date.today().isoformat(),
        "qs": filters.query_string,
    }


def _fmt_eur(value: Any) -> str:
    if value is None:
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:,.1f}M"
    if amount >= 1_000:
        return f"{amount / 1_000:,.0f}k"
    return f"{amount:,.0f}"


def _fmt_day(value: Any) -> str:
    if not value:
        return "—"
    return str(value)[:10]


def _deadline_class(value: Any) -> str:
    """Classify a deadline so the template can colour it without logic."""
    if not value:
        return "none"
    day = str(value)[:10]
    today = dt.date.today().isoformat()
    if day < today:
        return "expired"
    try:
        days = (dt.date.fromisoformat(day) - dt.date.today()).days
    except ValueError:
        return "none"
    return "soon" if days <= 14 else "open"


app = create_app()


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="procsignal.web")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--host", default=os.environ.get("PROCSIGNAL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"no database at {args.db}")
        print("run an ingest first:  python -m procsignal.ingest --days 30")
        return 1

    os.environ["PROCSIGNAL_DB"] = args.db
    print(f"serving {args.db} on http://{args.host}:{args.port}")
    uvicorn.run(
        "procsignal.web.app:app" if args.reload else create_app(args.db),
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
