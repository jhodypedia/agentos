"""In-process metrics registry rendered in Prometheus text exposition format.

Deliberately dependency-free: the platform needs counters, gauges and a
histogram with fixed buckets, and that is a small amount of code compared with
adding a client library and its transitive tree. The exposition format is the
stable Prometheus 0.0.4 text format, so any scraper works unchanged.

Metrics are per-process. With more than one API worker each process exposes its
own numbers and the scraper is expected to aggregate; nothing here claims to be
cluster-wide.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    body = ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(labels.items()))
    return "{" + body + "}"


@dataclass(slots=True)
class _Series:
    labels: dict[str, str]
    value: float = 0.0
    buckets: list[float] = field(default_factory=list)
    count: int = 0
    total: float = 0.0


class _Metric:
    kind: str = "untyped"

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._series: dict[tuple[tuple[str, str], ...], _Series] = {}

    def _series_for(self, labels: dict[str, str]) -> _Series:
        key = tuple(sorted(labels.items()))
        series = self._series.get(key)
        if series is None:
            series = _Series(labels=dict(labels))
            self._series[key] = series
        return series

    def _render_line(self, suffix: str, labels: dict[str, str], value: float) -> str:
        return f"{self.name}{suffix}{_format_labels(labels)} {value}"

    def render(self) -> Iterable[str]:
        raise NotImplementedError


class Counter(_Metric):
    kind = "counter"

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("counters can only increase")
        with self._lock:
            self._series_for({k: str(v) for k, v in labels.items()}).value += amount

    def value(self, **labels: str) -> float:
        with self._lock:
            series = self._series_for({k: str(v) for k, v in labels.items()})
            return series.value

    def render(self) -> Iterable[str]:
        with self._lock:
            for series in self._series.values():
                yield self._render_line("", series.labels, series.value)


class Gauge(_Metric):
    kind = "gauge"

    def set(self, value: float, **labels: str) -> None:
        with self._lock:
            self._series_for({k: str(v) for k, v in labels.items()}).value = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._series_for({k: str(v) for k, v in labels.items()}).value += amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._series_for({k: str(v) for k, v in labels.items()}).value -= amount

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._series_for({k: str(v) for k, v in labels.items()}).value

    def render(self) -> Iterable[str]:
        with self._lock:
            for series in self._series.values():
                yield self._render_line("", series.labels, series.value)


class Histogram(_Metric):
    kind = "histogram"

    def __init__(
        self, name: str, help_text: str, buckets: tuple[float, ...] = DEFAULT_BUCKETS
    ) -> None:
        super().__init__(name, help_text)
        self.buckets = tuple(sorted(buckets))

    def observe(self, value: float, **labels: str) -> None:
        label_map = {k: str(v) for k, v in labels.items()}
        with self._lock:
            series = self._series_for(label_map)
            if not series.buckets:
                series.buckets = [0.0] * len(self.buckets)
            # Only the first matching bucket is incremented; the cumulative counts
            # Prometheus expects are produced at render time. Incrementing every
            # bound at or above the value and then summing would count each
            # observation twice.
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    series.buckets[index] += 1
                    break
            series.count += 1
            series.total += value

    def render(self) -> Iterable[str]:
        with self._lock:
            for series in self._series.values():
                cumulative = 0.0
                for bound, count in zip(self.buckets, series.buckets, strict=True):
                    cumulative += count
                    yield self._render_line(
                        "_bucket", {**series.labels, "le": _format_bound(bound)}, cumulative
                    )
                yield self._render_line(
                    "_bucket", {**series.labels, "le": "+Inf"}, float(series.count)
                )
                yield self._render_line("_sum", series.labels, series.total)
                yield self._render_line("_count", series.labels, float(series.count))


def _format_bound(value: float) -> str:
    if math.isinf(value):
        return "+Inf"
    return repr(float(value))


class Registry:
    """A named collection of metrics plus the text renderer."""

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str) -> Counter:
        return self._get(name, lambda: Counter(name, help_text), Counter)

    def gauge(self, name: str, help_text: str) -> Gauge:
        return self._get(name, lambda: Gauge(name, help_text), Gauge)

    def histogram(
        self, name: str, help_text: str, buckets: tuple[float, ...] = DEFAULT_BUCKETS
    ) -> Histogram:
        return self._get(name, lambda: Histogram(name, help_text, buckets), Histogram)

    def _get[T: _Metric](self, name: str, factory: Callable[[], T], expected: type[T]) -> T:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                created = factory()
                self._metrics[name] = created
                return created
            if not isinstance(metric, expected):
                raise TypeError(f"metric '{name}' is already registered with another type")
            return metric

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            metrics = list(self._metrics.values())
        for metric in sorted(metrics, key=lambda item: item.name):
            lines.append(f"# HELP {metric.name} {_escape(metric.help_text)}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"

    def clear(self) -> None:
        """Drop every series. Used by tests; not called at runtime."""
        with self._lock:
            self._metrics.clear()


registry = Registry()

HTTP_REQUESTS = registry.counter(
    "agentos_http_requests_total", "HTTP requests handled, by method, route and status"
)
HTTP_DURATION = registry.histogram(
    "agentos_http_request_duration_seconds", "HTTP request latency in seconds"
)
HTTP_IN_FLIGHT = registry.gauge(
    "agentos_http_requests_in_flight", "HTTP requests currently being handled"
)
RATE_LIMITED = registry.counter(
    "agentos_rate_limited_total", "Requests rejected by the rate limiter, by scope"
)
TOOL_RUNS = registry.counter("agentos_tool_executions_total", "Tool executions by slug and outcome")
TOOL_DURATION = registry.histogram(
    "agentos_tool_duration_seconds", "Tool execution latency in seconds, by slug"
)
PROVIDER_CALLS = registry.counter(
    "agentos_provider_requests_total", "Model provider calls by provider and outcome"
)
PROVIDER_DURATION = registry.histogram(
    "agentos_provider_request_duration_seconds", "Model provider latency in seconds"
)
TASKS_BY_STATUS = registry.gauge(
    "agentos_tasks", "Tasks currently in each terminal or active status"
)
AGENTS_BY_STATUS = registry.gauge("agentos_agents", "Agents currently in each status")
TOKENS_TOTAL = registry.counter(
    "agentos_model_tokens_total", "Model tokens consumed, by model provider and token kind"
)
COST_TOTAL = registry.counter(
    "agentos_model_cost_usd_total", "Recorded model cost in USD, by model provider"
)
APPROVALS_PENDING = registry.gauge(
    "agentos_approvals_pending", "Approval requests waiting for a human decision"
)
WORKSPACE_QUOTA_REJECTED = registry.counter(
    "agentos_workspace_quota_rejected_total", "Writes or commands refused by the workspace quota"
)
SANDBOX_FALLBACK = registry.counter(
    "agentos_sandbox_fallback_total", "Command runs that fell back to host execution"
)
BACKGROUND_JOBS = registry.counter(
    "agentos_background_jobs_total", "Background maintenance jobs, by name and outcome"
)
TASK_DURATION = registry.histogram(
    "agentos_task_duration_seconds",
    "Wall-clock time a worker spent executing one task",
    buckets=(1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0),
)
