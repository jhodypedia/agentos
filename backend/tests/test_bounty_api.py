"""Integration tests for the bounty API and its approval-gated submission.

These run against PostgreSQL. make.fun is never contacted: every test injects a
stub client, so a passing suite never proves the live board was read — that is
verified separately against the real API.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from httpx import AsyncClient

from app.bounty.makefun import MakeFunClient
from app.core.config import get_settings
from app.db.enums import ApprovalStatus, CompetitionEntryStatus, CompetitionStatus, UserRole
from app.db.models import Approval, Competition, CompetitionEntry
from app.db.session import get_session_factory
from app.services import bounty as bounty_service
from app.services.users import create_user

pytestmark = pytest.mark.integration

BOARD: dict[str, Any] = {
    "items": [
        {
            "id": "cmp_1",
            "slug": "tiny-chess-01",
            "title": "Build the strongest chess engine in 4 KB of Python",
            "url": "/bounty/tiny-chess-01",
            "rewardAmount": 500,
            "deadline": "2027-01-01T00:00:00Z",
            "submissionCount": 3,
            "sponsor": {"name": "make.fun", "slug": "makefun", "isVerified": True},
            "category": "Games",
            "skills": ["python"],
            "description": "Four kilobytes.",
            "status": "open",
            "rewards": {"first": 300, "second": 120, "third": 80},
            "entryFeeUsd": 0,
            "potUsd": 500,
        },
        {
            "slug": "old-one",
            "title": "Finished thing",
            "rewardAmount": 100,
            "deadline": "2020-01-01T00:00:00Z",
            "status": "open",
        },
    ],
    "next_cursor": None,
}

REQUIREMENTS: dict[str, Any] = {
    "competition": {"slug": "tiny-chess-01", "status": "open", "harness": "program/v1"},
    "deliverables": [
        {"name": "repo_url", "kind": "form_field", "required": True, "description": "repo"},
        {"name": "commit_sha", "kind": "form_field", "required": True, "description": "sha"},
        {"name": "bundle", "kind": "file", "required": True, "description": "transcripts"},
    ],
    "checks": [
        {
            "check": "tests_pass",
            "phase": "grade",
            "title": "Tests pass",
            "description": "The harness suite passes.",
            "fix": "Run the suite.",
            "type": "binary",
        }
    ],
    "quota": {"max_per_day": 3, "max_entries_per_account": 10, "window": "24h"},
    "deadlines": {"entries_close": "2027-01-01T00:00:00Z", "result_due": "2027-01-05T00:00:00Z"},
    "payout_curve": {"rail": "usdc", "total_usd": 500, "places": []},
    "escrow": {"required": False},
    "how_to_submit": {
        "method": "POST",
        "href": "/api/competitions/tiny-chess-01/submissions",
        "content_type": "multipart/form-data",
        "fields": ["repo_url", "commit_sha", "bundle"],
    },
}


@pytest.fixture(autouse=True)
async def workspace_root(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="agentos-bounty-ws-"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> MakeFunClient:
    """A make.fun client wired to canned responses, installed as the default."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if path == "/api/competitions":
            return httpx.Response(200, json=BOARD)
        if path.endswith("/requirements"):
            return httpx.Response(200, json=REQUIREMENTS)
        if path.endswith("/quota"):
            return httpx.Response(200, json={"max_per_day": 3, "max_entries_per_account": 10})
        if path.endswith("/submissions") and request.method == "POST":
            return httpx.Response(202, json={"submission_id": "sub_abc", "claim_code": "claim"})
        if path.startswith("/api/submissions/"):
            return httpx.Response(
                200,
                json={
                    "status": "graded",
                    "score": 92.5,
                    "rank": 2,
                    "checks": [{"check": "tests_pass", "status": "pass"}],
                    "payout": {"amountUsd": 120},
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    client = MakeFunClient(
        base_url="https://make.fun",
        api_key="af_stub",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(bounty_service, "makefun_client", lambda: client)
    client.calls = calls  # type: ignore[attr-defined]
    return client


async def _token(client: AsyncClient, email: str, role: UserRole) -> dict[str, str]:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        await create_user(
            session, email=email, password="Str0ng-Passw0rd!", role=role, display_name="T"
        )
        await session.commit()
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def operator(app_client: AsyncClient) -> dict[str, str]:
    return await _token(app_client, "bounty-op@example.com", UserRole.OPERATOR)


@pytest.fixture
async def viewer(app_client: AsyncClient) -> dict[str, str]:
    return await _token(app_client, "bounty-viewer@example.com", UserRole.VIEWER)


async def _synced_competition(app_client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await app_client.post("/api/v1/bounties/competitions/sync", headers=headers, json={})
    assert response.status_code == 200, response.text
    listing = await app_client.get(
        "/api/v1/bounties/competitions/by-slug/tiny-chess-01", headers=headers
    )
    assert listing.status_code == 200, listing.text
    return listing.json()


# ------------------------------------------------------------------ discovery


async def test_sync_stores_the_board_verbatim(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    response = await app_client.post("/api/v1/bounties/competitions/sync", headers=operator)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == ["tiny-chess-01", "old-one"]
    assert body["total"] == 2

    listing = await app_client.get("/api/v1/bounties/competitions", headers=operator)
    items = {item["external_id"]: item for item in listing.json()}
    assert items["tiny-chess-01"]["reward_amount"] == "500.00"
    assert items["tiny-chess-01"]["status"] == "discovered"
    assert items["tiny-chess-01"]["url"] == "https://make.fun/bounty/tiny-chess-01"
    # The listing says "open" but the deadline has passed: reporting it open
    # would invite an entry the API will refuse.
    assert items["old-one"]["status"] == "closed"


async def test_sync_is_idempotent(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    await app_client.post("/api/v1/bounties/competitions/sync", headers=operator)
    second = await app_client.post("/api/v1/bounties/competitions/sync", headers=operator)
    assert second.status_code == 200
    assert second.json()["created"] == []
    assert set(second.json()["updated"]) == {"tiny-chess-01", "old-one"}

    listing = await app_client.get("/api/v1/bounties/competitions", headers=operator)
    assert len(listing.json()) == 2


async def test_viewer_can_read_but_not_sync(
    app_client: AsyncClient, viewer: dict[str, str]
) -> None:
    assert (
        await app_client.get("/api/v1/bounties/competitions", headers=viewer)
    ).status_code == 200
    forbidden = await app_client.post("/api/v1/bounties/competitions/sync", headers=viewer)
    assert forbidden.status_code == 403


async def test_unknown_slug_is_a_404(app_client: AsyncClient, viewer: dict[str, str]) -> None:
    response = await app_client.get("/api/v1/bounties/competitions/by-slug/nope-01", headers=viewer)
    assert response.status_code == 404


async def test_requirements_refresh_summarises_the_document(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    competition = await _synced_competition(app_client, operator)
    response = await app_client.post(
        f"/api/v1/bounties/competitions/{competition['id']}/requirements/refresh",
        headers=operator,
    )
    assert response.status_code == 200, response.text
    summary = response.json()["requirements_summary"]
    assert summary["transcripts_required"] is True
    assert summary["checks"][0]["check"] == "tests_pass"
    assert summary["checks"][0]["fix"] == "Run the suite."
    assert summary["quota"]["max_per_day"] == 3
    assert summary["submission"]["content_type"] == "multipart/form-data"

    detail = await app_client.get(
        f"/api/v1/bounties/competitions/{competition['id']}", headers=operator
    )
    assert detail.json()["requirements_summary"]["deadlines"]["entries_close"].startswith("2027")


async def test_quota_comes_from_the_platform(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    competition = await _synced_competition(app_client, operator)
    response = await app_client.get(
        f"/api/v1/bounties/competitions/{competition['id']}/quota", headers=operator
    )
    assert response.status_code == 200, response.text
    assert response.json()["max_per_day"] == 3


# ------------------------------------------------------------------ submission


async def _prepare(
    app_client: AsyncClient, headers: dict[str, str], competition_id: str
) -> dict[str, Any]:
    response = await app_client.post(
        f"/api/v1/bounties/competitions/{competition_id}/entries",
        headers=headers,
        json={
            "repo_url": "https://github.com/example/engine",
            "commit_sha": "a" * 40,
            "title": "Engine",
            "transcript_paths": ["transcripts/session-1.jsonl"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_prepare_creates_a_workspace_and_an_approval(
    app_client: AsyncClient,
    operator: dict[str, str],
    stub_client: MakeFunClient,
    workspace_root: Path,
) -> None:
    competition = await _synced_competition(app_client, operator)
    entry = await _prepare(app_client, operator, competition["id"])

    assert entry["status"] == "awaiting_approval"
    assert entry["approval_id"]
    assert entry["transcript_paths"] == ["transcripts/session-1.jsonl"]

    # Preparing must not touch the platform.
    assert not any(path.endswith("/submissions") for _method, path in stub_client.calls)  # type: ignore[attr-defined]

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        approval = await session.get(Approval, entry["approval_id"])
        assert approval is not None
        assert approval.status == ApprovalStatus.PENDING
        assert approval.action_type.value == "submit_competition"
        assert approval.payload["entry_id"] == entry["id"]

    projects = await app_client.get("/api/v1/projects", headers=operator)
    slugs = [item["slug"] for item in projects.json()]
    assert "bounty-tiny-chess-01" in slugs


async def test_submission_without_approval_is_refused(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    competition = await _synced_competition(app_client, operator)
    entry = await _prepare(app_client, operator, competition["id"])

    response = await app_client.post(
        f"/api/v1/bounties/entries/{entry['id']}/submit", headers=operator
    )
    assert response.status_code == 409
    assert "approval" in response.json()["error"]["message"].lower()
    assert not any(path.endswith("/submissions") for _method, path in stub_client.calls)  # type: ignore[attr-defined]


async def test_rejected_approval_blocks_submission(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    competition = await _synced_competition(app_client, operator)
    entry = await _prepare(app_client, operator, competition["id"])

    decision = await app_client.post(
        f"/api/v1/approvals/{entry['approval_id']}/decide",
        headers=operator,
        json={"approved": False, "note": "not good enough"},
    )
    assert decision.status_code == 200, decision.text

    response = await app_client.post(
        f"/api/v1/bounties/entries/{entry['id']}/submit", headers=operator
    )
    assert response.status_code == 409


async def test_approved_entry_is_submitted_and_tracked(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    competition = await _synced_competition(app_client, operator)
    entry = await _prepare(app_client, operator, competition["id"])

    decision = await app_client.post(
        f"/api/v1/approvals/{entry['approval_id']}/decide",
        headers=operator,
        json={"approved": True},
    )
    assert decision.status_code == 200, decision.text

    submitted = await app_client.post(
        f"/api/v1/bounties/entries/{entry['id']}/submit", headers=operator
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["status"] == "submitted"
    assert body["submission_id"] == "sub_abc"
    assert body["submitted_at"]

    refreshed = await app_client.post(
        f"/api/v1/bounties/entries/{entry['id']}/refresh", headers=operator
    )
    assert refreshed.status_code == 200, refreshed.text
    graded = refreshed.json()
    assert graded["status"] == "graded"
    assert graded["score"] == "92.5"
    assert graded["rank"] == 2
    assert graded["prize_usd"] in {"120", "120.00"}
    assert graded["checks"][0]["status"] == "pass"


async def test_refusal_is_recorded_with_its_check_verdicts(
    app_client: AsyncClient,
    operator: dict[str, str],
    stub_client: MakeFunClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    competition = await _synced_competition(app_client, operator)
    entry = await _prepare(app_client, operator, competition["id"])
    await app_client.post(
        f"/api/v1/approvals/{entry['approval_id']}/decide",
        headers=operator,
        json={"approved": True},
    )

    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "title": "Entry refused",
                "detail": "Pre-check failed.",
                "stage": "precheck",
                "checks": [{"check": "commit_is_head", "status": "fail", "reason": "not head"}],
            },
        )

    stub_client._client = httpx.AsyncClient(transport=httpx.MockTransport(refusing))

    response = await app_client.post(
        f"/api/v1/bounties/entries/{entry['id']}/submit", headers=operator
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "makefun_entry_refused"
    assert error["details"]["checks"][0]["check"] == "commit_is_head"

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        stored = await session.get(CompetitionEntry, entry["id"])
        assert stored is not None
        assert stored.status == CompetitionEntryStatus.REJECTED
        assert stored.checks[0]["reason"] == "not head"


async def test_closed_competition_refuses_new_entries(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    await app_client.post("/api/v1/bounties/competitions/sync", headers=operator)
    listing = await app_client.get(
        "/api/v1/bounties/competitions/by-slug/old-one", headers=operator
    )
    competition_id = listing.json()["id"]

    response = await app_client.post(
        f"/api/v1/bounties/competitions/{competition_id}/entries",
        headers=operator,
        json={"repo_url": "https://github.com/x/y", "commit_sha": "d" * 40},
    )
    assert response.status_code == 409


async def test_commit_sha_must_be_a_full_sha(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    competition = await _synced_competition(app_client, operator)
    response = await app_client.post(
        f"/api/v1/bounties/competitions/{competition['id']}/entries",
        headers=operator,
        json={"repo_url": "https://github.com/x/y", "commit_sha": "abc123"},
    )
    assert response.status_code == 422


async def test_entries_are_listed_per_competition(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    competition = await _synced_competition(app_client, operator)
    await _prepare(app_client, operator, competition["id"])

    response = await app_client.get(
        f"/api/v1/bounties/competitions/{competition['id']}/entries", headers=operator
    )
    assert response.status_code == 200
    assert len(response.json()) == 1

    everything = await app_client.get("/api/v1/bounties/entries", headers=operator)
    assert len(everything.json()) == 1
    filtered = await app_client.get(
        "/api/v1/bounties/entries", headers=operator, params={"status": "submitted"}
    )
    assert filtered.json() == []


# ------------------------------------------------------------------------ tools


async def test_tools_read_the_mirror_and_never_submit(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    from app.services.bounty_tools import makefun_resources
    from app.tools.base import ToolContext
    from app.tools.makefun import MAKEFUN_TOOLS

    await _synced_competition(app_client, operator)

    from app.db.session import get_session_factory

    resources = makefun_resources(get_session_factory(), project_id=None, user_id=None)
    context = ToolContext(
        permissions=frozenset({"tool:read", "bounty:write", "bounty:submit"}),
        resources=resources,
    )
    tools = {tool.slug: tool() for tool in MAKEFUN_TOOLS}

    listed = await tools["makefun_competitions"].run({}, context)
    assert listed.ok, listed.error
    slugs = [item["slug"] for item in listed.output["items"]]
    assert "tiny-chess-01" in slugs
    assert listed.output["source"] == "local mirror"

    details = await tools["makefun_competition_details"].run({"slug": "tiny-chess-01"}, context)
    assert details.ok, details.error
    assert details.output["requirements"]["checks"][0]["check"] == "tests_pass"

    prepared = await tools["makefun_prepare_entry"].run(
        {
            "slug": "tiny-chess-01",
            "repo_url": "https://github.com/example/engine",
            "commit_sha": "e" * 40,
        },
        context,
    )
    assert prepared.ok, prepared.error
    assert prepared.output["status"] == "awaiting_approval"
    assert not any(path.endswith("/submissions") for _method, path in stub_client.calls)  # type: ignore[attr-defined]

    from app.tools.errors import ApprovalRequiredError

    # Critical risk: the runtime refuses it outright without a per-run approval.
    with pytest.raises(ApprovalRequiredError):
        await tools["makefun_submit_entry"].run({"entry_id": prepared.output["entry_id"]}, context)

    # Even with the tool unlocked, the entry's own approval record still gates it.
    unlocked = ToolContext(
        permissions=frozenset({"tool:read", "bounty:submit"}),
        approvals=frozenset({"makefun_submit_entry"}),
        resources=resources,
    )
    refused = await tools["makefun_submit_entry"].run(
        {"entry_id": prepared.output["entry_id"]}, unlocked
    )
    assert refused.ok is False
    assert "approval" in (refused.error or "").lower()

    # Approve the entry, then the same call goes through.
    async with get_session_factory()() as session:
        approval = await session.get(Approval, UUID(prepared.output["approval_id"]))
        assert approval is not None
        approval.status = ApprovalStatus.APPROVED
        await session.commit()

    done = await tools["makefun_submit_entry"].run(
        {"entry_id": prepared.output["entry_id"]}, unlocked
    )
    assert done.ok, done.error
    assert done.output["submission_id"] == "sub_abc"


async def test_submit_tool_is_gated_by_the_runtime(
    app_client: AsyncClient, operator: dict[str, str], stub_client: MakeFunClient
) -> None:
    from app.tools.base import ToolContext
    from app.tools.makefun import SubmitEntryTool

    tool = SubmitEntryTool()
    unapproved = ToolContext(permissions=frozenset({"tool:read", "bounty:submit"}), resources={})
    from app.tools.errors import ApprovalRequiredError

    with pytest.raises(ApprovalRequiredError):
        tool.check_permissions(unapproved)
    assert tool.requires_approval is True


async def test_tools_report_missing_platform_access() -> None:
    from app.tools.base import ToolContext
    from app.tools.makefun import CompetitionsTool

    result = await CompetitionsTool().run({}, ToolContext(permissions=frozenset({"tool:read"})))
    assert result.ok is False
    assert "not available" in (result.error or "")


# ------------------------------------------------------------ service helpers


def test_status_mapping_never_reports_a_past_deadline_as_open() -> None:
    from datetime import UTC, datetime, timedelta

    from app.db.enums import CompetitionStatus

    future = datetime.now(UTC) + timedelta(days=1)
    past = datetime.now(UTC) - timedelta(days=1)

    assert bounty_service._status_for("open", future) == CompetitionStatus.DISCOVERED
    assert bounty_service._status_for("open", past) == CompetitionStatus.CLOSED
    assert bounty_service._status_for("open", None) == CompetitionStatus.DISCOVERED
    assert bounty_service._status_for("resolved", future) == CompetitionStatus.CLOSED
    assert bounty_service._status_for("draft", future) == CompetitionStatus.DISCOVERED


def test_summary_of_an_empty_document_does_not_invent_requirements() -> None:
    summary = bounty_service.requirements_summary({})
    assert summary["checks"] == []
    assert summary["deliverables"] == []
    assert summary["transcripts_required"] is False


async def test_missing_key_on_a_write_is_reported_not_retried(
    app_client: AsyncClient, operator: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with get_session_factory()() as session:
        program = await bounty_service.get_program(session)
        session.add(
            Competition(
                program_id=program.id,
                external_id="manual-01",
                title="Manual",
                status=CompetitionStatus.DISCOVERED,
            )
        )
        await session.commit()

    listing = await app_client.get("/api/v1/bounties/competitions", headers=operator)
    created = next(item for item in listing.json() if item["external_id"] == "manual-01")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": [{"message": "missing api key"}]})

    monkeypatch.setattr(
        bounty_service,
        "makefun_client",
        lambda: MakeFunClient(
            base_url="https://make.fun",
            api_key=None,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
    )
    response = await app_client.get(
        f"/api/v1/bounties/competitions/{created['id']}/quota", headers=operator
    )
    assert response.status_code == 502
    assert "missing api key" in response.json()["error"]["message"]


def test_workspace_slug_is_bounded() -> None:
    slug = f"bounty-{'x' * 200}"[:60].rstrip("-")
    assert len(slug) <= 60
    assert os.sep not in slug
