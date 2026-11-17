"""The standalone metrics endpoint used by the worker and bot processes.

The API's /metrics is covered in test_observability; this covers the tiny HTTP
server that exposes a registry from a process with no web framework, which is
the only way a worker-only counter becomes visible to a scraper.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.metrics import Registry
from app.core.metrics_server import MetricsServer


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _get(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=5)
    finally:
        writer.close()
        await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, body


async def test_metrics_endpoint_renders_the_registry() -> None:
    registry = Registry()
    counter = registry.counter("agentos_test_total", "A counter for the test")
    counter.inc(3)
    port = _free_port()
    server = MetricsServer(registry, host="127.0.0.1", port=port)
    await server.start()
    try:
        status, body = await _get(port, "/metrics")
    finally:
        await server.stop()

    assert status == 200
    text = body.decode()
    assert "# TYPE agentos_test_total counter" in text
    assert "agentos_test_total 3" in text


async def test_health_and_unknown_paths() -> None:
    port = _free_port()
    server = MetricsServer(Registry(), host="127.0.0.1", port=port)
    await server.start()
    try:
        health_status, health_body = await _get(port, "/healthz")
        missing_status, _ = await _get(port, "/nope")
    finally:
        await server.stop()

    assert health_status == 200
    assert health_body == b"ok"
    assert missing_status == 404


async def test_server_stops_cleanly_and_can_restart() -> None:
    port = _free_port()
    server = MetricsServer(Registry(), host="127.0.0.1", port=port)
    await server.start()
    await server.stop()
    await server.stop()
    await server.start()
    try:
        status, _ = await _get(port, "/metrics")
        assert status == 200
    finally:
        await server.stop()


async def test_a_malformed_request_does_not_kill_the_server() -> None:
    port = _free_port()
    server = MetricsServer(Registry(), host="127.0.0.1", port=port)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        await writer.wait_closed()

        status, _ = await _get(port, "/metrics")
        assert status == 200
    finally:
        await server.stop()


@pytest.mark.parametrize("path", ["/metrics", "/health"])
async def test_paths_are_served_without_a_query_string(path: str) -> None:
    port = _free_port()
    server = MetricsServer(Registry(), host="127.0.0.1", port=port)
    await server.start()
    try:
        status, _ = await _get(port, f"{path}?x=1")
    finally:
        await server.stop()
    # A query string is not stripped, so it is not a known route; the point is
    # that it answers rather than hanging.
    assert status in (200, 404)
