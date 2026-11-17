"""Unit tests for the make.fun HTTP client.

The client is the only place the platform talks to make.fun, so these tests pin
down what it does with each documented response shape: success, cursor paging,
RFC 9457 problem documents with per-check verdicts, 401 on a write, and
transport failures. No network access is involved.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.bounty.makefun import MakeFunClient, MakeFunError, MakeFunProblem


def _client(handler: Any, *, api_key: str | None = "af_test") -> MakeFunClient:
    return MakeFunClient(
        base_url="https://make.fun",
        api_key=api_key,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_list_competitions_passes_filters_and_returns_cursor() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={"items": [{"slug": "tiny-chess-01", "status": "open"}], "next_cursor": "c2"},
        )

    async with _client(handler) as client:
        body = await client.list_competitions(limit=5, status="open")

    assert seen["path"] == "/api/competitions"
    assert seen["params"] == {"limit": "5", "status": "open"}
    assert seen["key"] == "af_test"
    assert body["next_cursor"] == "c2"
    assert body["items"][0]["slug"] == "tiny-chess-01"


async def test_read_routes_work_without_a_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json={"items": []})

    async with _client(handler, api_key=None) as client:
        assert await client.list_competitions() == {"items": []}


async def test_requirements_are_returned_verbatim() -> None:
    document = {
        "competition": {"slug": "tiny-chess-01", "status": "open", "harness": "program/v1"},
        "deliverables": [{"name": "repo_url", "kind": "form_field", "required": True}],
        "checks": [{"check": "tests_pass", "phase": "grade", "fix": "run the suite"}],
        "deadlines": {"entries_close": "2026-12-01T00:00:00Z"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/competitions/tiny-chess-01/requirements"
        return httpx.Response(200, json=document)

    async with _client(handler) as client:
        assert await client.get_requirements("tiny-chess-01") == document


async def test_refused_submission_surfaces_each_check_verdict() -> None:
    problem = {
        "type": "https://make.fun/problems/entry-refused",
        "title": "Entry refused",
        "status": 422,
        "stage": "precheck",
        "detail": "The entry failed the pre-check.",
        "checks": [
            {"check": "commit_is_head", "status": "fail", "reason": "not on a branch"},
            {"check": "transcripts_present", "status": "pass", "reason": ""},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(422, json=problem)

    async with _client(handler) as client:
        with pytest.raises(MakeFunProblem) as excinfo:
            await client.submit_entry(
                "tiny-chess-01", repo_url="https://github.com/x/y", commit_sha="a" * 40
            )

    problem_exc = excinfo.value
    assert problem_exc.stage == "precheck"
    assert [check["check"] for check in problem_exc.checks] == [
        "commit_is_head",
        "transcripts_present",
    ]
    assert problem_exc.details["checks"][0]["reason"] == "not on a branch"


async def test_submission_sends_form_fields_and_transcript_files() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(202, json={"submission_id": "sub_123", "claim_code": "claim-1"})

    async with _client(handler) as client:
        body = await client.submit_entry(
            "tiny-chess-01",
            repo_url="https://github.com/x/y",
            commit_sha="b" * 40,
            title="Engine",
            transcripts=[("session.txt", b"transcript-bytes", "text/plain")],
        )

    assert body["submission_id"] == "sub_123"
    assert "multipart/form-data" in captured["content_type"]
    assert 'name="repo_url"' in captured["body"]
    assert 'name="transcripts"' in captured["body"]
    assert 'filename="session.txt"' in captured["body"]


async def test_entry_fee_refusal_is_a_problem_not_a_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={"title": "Entry fee required", "detail": "Pay the entry fee first."},
        )

    async with _client(handler) as client:
        with pytest.raises(MakeFunProblem) as excinfo:
            await client.submit_entry(
                "tiny-chess-01", repo_url="https://github.com/x/y", commit_sha="c" * 40
            )

    assert excinfo.value.upstream_status == 402
    assert excinfo.value.status_code == 422
    assert "fee" in excinfo.value.message.lower()


async def test_write_without_key_reports_authentication_problem() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": [{"message": "missing api key"}]})

    async with _client(handler, api_key=None) as client:
        with pytest.raises(MakeFunError) as excinfo:
            await client.get_quota("tiny-chess-01")

    assert not isinstance(excinfo.value, MakeFunProblem)
    assert "missing api key" in excinfo.value.message


async def test_expired_key_on_submit_is_not_a_refused_entry() -> None:
    """A lapsed key says nothing about the entry and must not be recorded as a rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"success": False, "error": [{"message": "sign in required"}]}
        )

    async with _client(handler, api_key=None) as client:
        with pytest.raises(MakeFunError) as excinfo:
            await client.submit_entry(
                "tiny-chess-01", repo_url="https://github.com/x/y", commit_sha="f" * 40
            )

    assert not isinstance(excinfo.value, MakeFunProblem)
    assert excinfo.value.details["status_code"] == 401


async def test_server_failure_on_submit_is_not_a_refused_entry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "upstream down"})

    async with _client(handler) as client:
        with pytest.raises(MakeFunError) as excinfo:
            await client.submit_entry(
                "tiny-chess-01", repo_url="https://github.com/x/y", commit_sha="f" * 40
            )

    assert not isinstance(excinfo.value, MakeFunProblem)


async def test_server_error_is_retried_then_raised() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"detail": "unavailable"})

    async with _client(handler) as client:
        with pytest.raises(MakeFunError):
            await client.list_competitions()

    assert len(calls) == 3


async def test_retry_after_header_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    responses = [httpx.Response(429, headers={"retry-after": "2"}), httpx.Response(200, json={})]

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("app.bounty.makefun.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with _client(handler) as client:
        assert await client.get_competition("x") == {}

    assert delays == [2.0]


async def test_redirect_is_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "make.fun" and request.url.path == "/api/competitions":
            return httpx.Response(307, headers={"location": "https://evil.example/api"})
        return httpx.Response(200, json={"items": ["leaked"]})

    async with _client(handler) as client:
        with pytest.raises(MakeFunError):
            await client.list_competitions()


async def test_transport_failure_becomes_a_makefun_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with _client(handler) as client:
        with pytest.raises(MakeFunError) as excinfo:
            await client.get_competition("x")

    assert "transport error" in excinfo.value.message


async def test_error_body_without_json_is_reported_not_crashed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>gateway</html>")

    async with _client(handler) as client:
        with pytest.raises(MakeFunError):
            await client.get_competition("x")


async def test_submission_status_is_read_from_the_documented_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/submissions/sub_123/status"
        return httpx.Response(200, json={"status": "graded", "score": 91.5})

    async with _client(handler) as client:
        assert await client.get_submission_status("sub_123") == {"status": "graded", "score": 91.5}


async def test_api_key_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    with caplog.at_level("DEBUG"):
        async with _client(handler, api_key="af_supersecret") as client:
            with pytest.raises(MakeFunError):
                await client.list_competitions()

    assert "af_supersecret" not in caplog.text
    assert "af_supersecret" not in json.dumps([r.getMessage() for r in caplog.records])
