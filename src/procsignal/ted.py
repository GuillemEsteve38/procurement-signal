"""TED Search API v3 client.

The API is public and unauthenticated. Its errors are well structured, which
lets us draw the distinction that matters for an unattended ingest: a *query*
error is a bug in our code and must fail loudly, while a *transport* error is
weather and should be retried.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import httpx

LOG = logging.getLogger(__name__)

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

#: Retried with backoff. Everything else is raised immediately.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: TED rejects a request whose ``fields x limit`` product exceeds this, with
#: SEARCH_FIELDS_PER_PAGE_EXCEEDS_MAX_LIMIT. Undocumented; found by asking for
#: 45 fields at a page size of 250. The client derives the page size from it
#: rather than making the caller do the arithmetic every time a field is added.
MAX_FIELDS_PER_PAGE = 10_000


class TedError(RuntimeError):
    """Base class for TED API failures."""


class TedQueryError(TedError):
    """The API rejected our request. Not retryable — the request is wrong.

    TED reports these with a machine-readable type and, for expert-query
    problems, the offending field name and its position in the query string.
    """

    def __init__(self, message: str, *, payload: Any = None) -> None:
        super().__init__(message)
        self.payload = payload

    @classmethod
    def from_response(cls, response: httpx.Response) -> "TedQueryError":
        try:
            body = response.json()
        except ValueError:
            return cls(f"HTTP {response.status_code}: {response.text[:400]}")

        # Two different error shapes come back from this endpoint: expert-query
        # errors use a dict, request-validation errors use a list.
        err = body.get("error")
        detail = body.get("message", "")
        if isinstance(err, dict):
            where = err.get("location") or {}
            field = err.get("fieldName")
            value = err.get("fieldValue")
            pos = where.get("beginColumn")
            detail = (
                f"{err.get('type', 'QUERY_ERROR')}: {detail}"
                f" (field={field!r} value={value!r} column={pos})"
            )
        elif isinstance(err, list):
            parts = [
                f"{e.get('field')}: {e.get('message')}"
                for e in err
                if isinstance(e, dict)
            ]
            detail = f"{detail} [{'; '.join(parts)}]"
        return cls(f"HTTP {response.status_code} {detail}", payload=body)


class TedTransientError(TedError):
    """Retryable failure that outlived the retry budget."""


@dataclass
class Page:
    """One page of search results."""

    notices: list[dict[str, Any]]
    total: int
    next_token: str | None
    timed_out: bool

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> "Page":
        return cls(
            notices=body.get("notices") or [],
            total=int(body.get("totalNoticeCount") or 0),
            next_token=body.get("iterationNextToken") or None,
            timed_out=bool(body.get("timedOut")),
        )


class TedClient:
    """Thin, retrying client over ``POST /v3/notices/search``."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        max_retries: int = 5,
        user_agent: str = "procurement-signal/0.1 (+side-project)",
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"Content-Type": "application/json", "User-Agent": user_agent},
        )
        self.max_retries = max_retries

    def __enter__(self) -> "TedClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- single request ----------------------------------------------------

    def search(
        self,
        query: str,
        fields: list[str] | tuple[str, ...],
        *,
        limit: int = 250,
        iteration_token: str | None = None,
    ) -> Page:
        field_list = list(fields)
        effective_limit = clamp_limit(limit, len(field_list))
        if effective_limit != limit:
            LOG.debug(
                "page size %d -> %d to stay under the %d fields-per-page cap "
                "(%d fields requested)",
                limit,
                effective_limit,
                MAX_FIELDS_PER_PAGE,
                len(field_list),
            )

        body: dict[str, Any] = {
            "query": query,
            "fields": field_list,
            "limit": effective_limit,
            "paginationMode": "ITERATION",
        }
        if iteration_token:
            body["iterationNextToken"] = iteration_token
        return Page.from_json(self._post(body))

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post(SEARCH_URL, json=body)
            except httpx.HTTPError as exc:
                last_exc = exc
                LOG.warning(
                    "ted request failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
            else:
                if response.status_code == 200:
                    return response.json()

                if response.status_code in RETRY_STATUS:
                    last_exc = TedTransientError(
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                    LOG.warning(
                        "ted returned %d (attempt %d/%d)",
                        response.status_code,
                        attempt + 1,
                        self.max_retries + 1,
                    )
                    retry_after = _retry_after_seconds(response)
                    if retry_after is not None:
                        time.sleep(retry_after)
                        continue
                else:
                    # 4xx that is not rate limiting: our request is wrong.
                    # Retrying would just make the same mistake more often.
                    raise TedQueryError.from_response(response)

            if attempt < self.max_retries:
                time.sleep(_backoff_seconds(attempt))

        raise TedTransientError(
            f"gave up after {self.max_retries + 1} attempts"
        ) from last_exc

    # -- pagination --------------------------------------------------------

    def iter_notices(
        self,
        query: str,
        fields: list[str] | tuple[str, ...],
        *,
        limit: int = 250,
        max_pages: int | None = None,
        on_page: Callable[[int, Page], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every notice matching ``query``, following the scroll token.

        TED's ITERATION mode hands back an opaque ``iterationNextToken``; the
        scroll ends when it comes back null. ``on_page`` is called once per
        page so callers can log progress and spot a server-side ``timedOut``
        without having to count notices themselves.
        """
        token: str | None = None
        page_no = 0

        while True:
            page = self.search(query, fields, limit=limit, iteration_token=token)
            page_no += 1

            if on_page is not None:
                on_page(page_no, page)

            if page.timed_out:
                # The server truncated this page's result set. Surfacing it is
                # the point: silently yielding a short page would look like a
                # successful run that quietly lost notices.
                raise TedTransientError(
                    f"TED reported timedOut on page {page_no}; "
                    "narrow the date window and retry"
                )

            yield from page.notices

            token = page.next_token
            if not token or not page.notices:
                return
            if max_pages is not None and page_no >= max_pages:
                LOG.warning(
                    "stopping at max_pages=%d; %d notices matched the query",
                    max_pages,
                    page.total,
                )
                return


def clamp_limit(limit: int, field_count: int) -> int:
    """Largest page size that keeps ``fields x limit`` under TED's cap."""
    if field_count <= 0:
        return max(1, limit)
    return max(1, min(limit, MAX_FIELDS_PER_PAGE // field_count))


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with full jitter, capped at 30s."""
    return min(30.0, (2**attempt) * (0.5 + random.random()))


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return min(60.0, float(raw))
    except ValueError:
        return None
