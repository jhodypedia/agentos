#!/usr/bin/env python3
"""Load test the AgentOS API and summarise what it reported.

Usage:
    uv run python scripts/loadtest.py --url http://localhost:8000 \\
        --email admin@example.com --password '...' --requests 400 --concurrency 20

What it measures, and what it does not:

* every request is a real HTTP call against a running API;
* the summary is computed from the responses actually received, so an error rate
  and a latency distribution are reported even when everything fails;
* it only issues GETs against read endpoints. Write endpoints are deliberately
  excluded: a load test that creates tasks spends real model budget.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

READ_PATHS = (
    "/api/v1/meta",
    "/api/v1/dashboard/summary",
    "/api/v1/agents",
    "/api/v1/tasks?limit=20",
    "/api/v1/approvals?limit=20",
)


@dataclass
class Sample:
    path: str
    status: int
    duration_ms: float
    error: str | None = None


@dataclass
class Result:
    samples: list[Sample] = field(default_factory=list)

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)

    def summary(self) -> dict[str, object]:
        if not self.samples:
            return {"requests": 0}
        durations = sorted(sample.duration_ms for sample in self.samples)
        failures = [sample for sample in self.samples if sample.error or sample.status >= 400]
        return {
            "requests": len(self.samples),
            "failures": len(failures),
            "error_rate": round(len(failures) / len(self.samples), 4),
            "duration_mean_ms": round(statistics.fmean(durations), 1),
            "duration_p50_ms": round(durations[len(durations) // 2], 1),
            "duration_p95_ms": round(durations[int(len(durations) * 0.95) - 1], 1),
            "duration_p99_ms": round(durations[int(len(durations) * 0.99) - 1], 1),
            "duration_max_ms": round(durations[-1], 1),
            "status_counts": _counts(sample.status for sample in self.samples),
        }


def _counts(values: object) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:  # type: ignore[union-attr]
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items()))


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return str(response.json()["access_token"])


async def _worker(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    result: Result,
    queue: asyncio.Queue[str],
) -> None:
    while True:
        try:
            path = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        started = time.perf_counter()
        try:
            response = await client.get(path, headers=headers)
            duration = (time.perf_counter() - started) * 1000
            result.add(Sample(path=path, status=response.status_code, duration_ms=duration))
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            result.add(
                Sample(
                    path=path,
                    status=0,
                    duration_ms=duration,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )


async def run(args: argparse.Namespace) -> int:
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.url, timeout=args.timeout, limits=limits) as client:
        headers: dict[str, str] = {}
        if args.email:
            token = await _login(client, args.email, args.password or "")
            headers["Authorization"] = f"Bearer {token}"

        queue: asyncio.Queue[str] = asyncio.Queue()
        for index in range(args.requests):
            queue.put_nowait(READ_PATHS[index % len(READ_PATHS)])

        result = Result()
        started = time.perf_counter()
        await asyncio.gather(
            *(_worker(client, headers, result, queue) for _ in range(args.concurrency))
        )
        elapsed = time.perf_counter() - started

    summary = result.summary()
    print(f"Target       : {args.url}")
    print(f"Elapsed      : {elapsed:.2f}s")
    for key, value in summary.items():
        print(f"{key:13}: {value}")

    failures = [sample for sample in result.samples if sample.error][:5]
    for sample in failures:
        print(f"  error on {sample.path}: {sample.error}")

    if summary.get("failures"):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load test the AgentOS read API")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


if __name__ == "__main__":
    sys.exit(asyncio.run(run(build_parser().parse_args())))
