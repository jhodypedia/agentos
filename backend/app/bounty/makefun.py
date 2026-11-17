"""HTTP client for the official make.fun agent API.

Source of truth: https://make.fun/skill.md and
https://make.fun/api/openapi.public.json. Every route used here appears in that
document; nothing is guessed.

Three properties of the real API shape this client:

* Errors come back as ``{"success": false, "error": [{"message": ...}]}``, and a
  refused entry is an RFC 9457 problem document with per-check verdicts.
* Lists are cursor pages: ``next_cursor`` is passed back verbatim.
* Read routes work without a key at a lower rate limit; writes require
  ``x-api-key``. A 401 on a write means the key lapsed (it lasts 30 days), which
  is a deployment problem, not a bug to retry around.

The client never logs or returns the API key, and it never follows a redirect
off the configured origin: this endpoint decides whether a submission is
accepted, so a redirect would silently move that decision somewhere else.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from app.core.exceptions import ExternalServiceError

logger = structlog.get_logger("agentos.bounty.makefun")

DEFAULT_BASE_URL = "https://make.fun"
DEFAULT_TIMEOUT_SECONDS = 30.0
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class MakeFunError(ExternalServiceError):
    """The make.fun API refused a call or could not be reached."""

    code = "makefun_error"
    message = "The make.fun API call failed"


class MakeFunProblem(MakeFunError):
    """A refused entry, as the API's RFC 9457 problem document describes it.

    The per-check verdicts are the useful part: they say which rule failed and
    what the fix is, which is exactly what an operator needs to act on.

    The HTTP status our API returns is ``422``: the platform refused *our*
    payload, so the caller has something to fix. The platform's own status is
    kept separately as ``upstream_status`` (``402`` for an unpaid entry fee,
    for instance) because it says which condition was hit.
    """

    code = "makefun_entry_refused"
    status_code = 422
    message = "The make.fun platform refused the entry"

    def __init__(
        self,
        message: str,
        *,
        checks: list[dict[str, Any]] | None = None,
        stage: str | None = None,
        status_code: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            details={
                "checks": checks or [],
                "stage": stage,
                "upstream_status": status_code,
                "detail": detail or {},
            },
        )
        self.checks = checks or []
        self.stage = stage
        self.upstream_status = status_code
        self.problem = detail or {}


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        errors = body.get("error")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message") or first)
            return str(first)
        if isinstance(errors, str):
            return errors
        if body.get("detail"):
            return str(body["detail"])
    return "The make.fun API returned an error"


class MakeFunClient:
    """Minimal typed wrapper over the documented make.fun routes."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------- lifecycle
    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> MakeFunClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -------------------------------------------------------------- requests
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "AgentOS/0.1 (+bounty:makefun)"}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
        expected_problem: bool = False,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urlencode(clean)}"
        client = self._client_or_create()

        for attempt in range(self._max_retries + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    params=None,
                    json=json,
                    data=data,
                    files=files,
                    headers=self._headers(),
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                logger.warning("makefun_timeout", method=method, path=path, attempt=attempt)
                if attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                raise MakeFunError(f"make.fun did not answer within {self._timeout:.0f}s") from exc
            except httpx.HTTPError as exc:
                logger.warning("makefun_transport_error", path=path, error=str(exc))
                raise MakeFunError(f"make.fun transport error: {exc}") from exc

            body = self._parse(response)

            if 300 <= response.status_code < 400:
                # Following it would move the decision to another host, and
                # treating it as success would report a submission that never
                # happened. Neither is acceptable for this endpoint.
                raise MakeFunError(
                    f"make.fun answered with a redirect (HTTP {response.status_code})",
                    details={"status_code": response.status_code},
                )
            if response.status_code < 300:
                return body

            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                delay = self._retry_after(response, attempt)
                logger.info("makefun_retry", path=path, status=response.status_code, delay=delay)
                await asyncio.sleep(delay)
                continue

            # Authentication is checked first and never modelled as a refused
            # entry. A lapsed key says nothing about the entry's quality:
            # recording it as a rejection would mark a perfectly good submission
            # as judged and failed.
            if response.status_code in {401, 403}:
                raise MakeFunError(
                    _error_message(body),
                    details={"status_code": response.status_code},
                )
            # A refusal is a 4xx problem document. A 5xx on submit is the
            # platform failing, not the entry being wrong, so it stays a plain
            # error and the entry is not marked rejected.
            if expected_problem and 400 <= response.status_code < 500:
                raise self._problem(response, body)
            raise MakeFunError(
                f"{_error_message(body)} (HTTP {response.status_code})",
                details={"status_code": response.status_code},
            )

        raise MakeFunError("make.fun request failed")

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            return {"raw": response.text[:2000]}
        return body if isinstance(body, dict) else {"data": body}

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("retry-after")
        if header:
            try:
                return min(30.0, max(1.0, float(header)))
            except ValueError:
                pass
        return float(min(30, 2**attempt))

    @staticmethod
    def _problem(response: httpx.Response, body: dict[str, Any]) -> MakeFunProblem:
        raw_checks = body.get("checks")
        checks: list[Any] = raw_checks if isinstance(raw_checks, list) else []
        normalised = [
            {
                "check": str(item.get("check", "")),
                "status": str(item.get("status", "")),
                "reason": str(item.get("reason", "")),
                "fix": item.get("fix"),
            }
            for item in checks
            if isinstance(item, dict)
        ]
        message = _error_message(body) or str(body.get("title") or "The entry was refused")
        return MakeFunProblem(
            message,
            checks=normalised,
            stage=body.get("stage"),
            status_code=response.status_code,
            detail=body,
        )

    # ------------------------------------------------------------ endpoints
    async def list_competitions(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        status: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """``GET /api/competitions`` — the board, one cursor page."""
        return await self._request(
            "GET",
            "/api/competitions",
            params={
                "limit": str(limit),
                "cursor": cursor,
                "status": status,
                "category": category,
            },
        )

    async def get_competition(self, slug: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/competitions/{slug}")

    async def get_requirements(self, slug: str) -> dict[str, Any]:
        """``GET /api/competitions/{slug}/requirements`` — deliverables and checks."""
        return await self._request("GET", f"/api/competitions/{slug}/requirements")

    async def get_quota(self, slug: str) -> dict[str, Any]:
        """Entries left today and in the competition for this account."""
        return await self._request("GET", f"/api/competitions/{slug}/quota")

    async def get_leaderboard(self, slug: str, *, phase: str | None = None) -> dict[str, Any]:
        return await self._request(
            "GET", f"/api/competitions/{slug}/leaderboard", params={"phase": phase}
        )

    async def agent_home(self) -> dict[str, Any]:
        return await self._request("GET", "/api/agents/me")

    async def submit_entry(
        self,
        slug: str,
        *,
        repo_url: str,
        commit_sha: str,
        title: str | None = None,
        entry_message: str | None = None,
        entry_signature: str | None = None,
        transcripts: list[tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/competitions/{slug}/submissions`` (multipart).

        A submission is the irreversible, externally visible act; the caller
        must have an approved approval record before reaching this method.

        ``transcripts`` are ``(filename, content, content_type)`` triples sent
        as repeated ``transcripts`` parts; the API accepts either that or a
        single ``bundle`` tarball.
        """
        data: dict[str, Any] = {"repo_url": repo_url, "commit_sha": commit_sha}
        if title:
            data["title"] = title[:200]
        if entry_message:
            data["entry_message"] = entry_message[:2064]
        if entry_signature:
            data["entry_signature"] = entry_signature[:128]

        files = (
            [
                ("transcripts", (name, content, content_type))
                for name, content, content_type in transcripts
            ]
            if transcripts
            else None
        )
        return await self._request(
            "POST",
            f"/api/competitions/{slug}/submissions",
            data=data,
            files=files,
            expected_problem=True,
        )

    async def get_submission_status(self, submission_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/submissions/{submission_id}/status")
