"""A minimal HTTP server that exposes ``/metrics`` for background processes.

The API serves metrics through FastAPI, but the worker and the bot are plain
asyncio processes with no HTTP server of their own. Incrementing a counter there
and never exposing it is dead code, and a worker-only metric (tasks executed,
leases reaped) is exactly the number an operator wants when the queue looks
stuck.

This is a deliberately tiny HTTP/1.1 responder: two routes, no keep-alive
chatter, no request parsing beyond the path. Prometheus' scraper only needs
``GET /metrics`` and a ``200``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.core.metrics import Registry

logger = structlog.get_logger("agentos.metrics_server")


class MetricsServer:
    """Serves a metrics registry over HTTP on ``host:port``."""

    # Binding to every interface is intentional: the scraper may live on another
    # host, and the port is only published where the operator chooses.
    def __init__(
        self,
        registry: Registry,
        *,
        # Bound to every interface on purpose: the metrics port is reachable
        # only on the Compose network, and a host-bound default would make the
        # worker's metrics unreachable from a scraper in another container.
        host: str = "0.0.0.0",  # noqa: S104
        port: int = 9101,
    ) -> None:
        self._registry = registry
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port, reuse_address=True
        )
        logger.info("metrics_server_started", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("metrics_server_stopped", port=self._port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            # Drain headers so the client is not left waiting on a closed socket.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
            path = request.split(b" ")[1].decode("latin-1") if b" " in request else "/"
            if path == "/metrics":
                body = self._registry.render().encode()
                status = b"200 OK"
                content_type = b"text/plain; version=0.0.4; charset=utf-8"
            elif path in ("/health", "/healthz"):
                body = b"ok"
                status = b"200 OK"
                content_type = b"text/plain"
            else:
                body = b"not found"
                status = b"404 Not Found"
                content_type = b"text/plain"
            writer.write(
                b"HTTP/1.1 " + status + b"\r\n"
                b"Content-Type: " + content_type + b"\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionError, IndexError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
