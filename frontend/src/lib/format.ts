export function cx(...values: (string | false | null | undefined)[]): string {
  return values.filter(Boolean).join(" ");
}

export function formatUsd(value: string | number | null | undefined): string {
  const numeric = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(numeric)) return "$0.00";
  if (numeric === 0) return "$0.00";
  if (Math.abs(numeric) < 0.01) return `$${numeric.toFixed(6)}`;
  return `$${numeric.toFixed(2)}`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value == null) return "0";
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value).getTime();
  if (Number.isNaN(date)) return "—";
  const seconds = Math.round((Date.now() - date) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function truncate(value: string, length = 120): string {
  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}

export type Tone = "neutral" | "ok" | "warn" | "danger" | "accent";

export function statusTone(status: string): Tone {
  switch (status) {
    case "completed":
    case "approved":
    case "active":
    case "ok":
    case "confirmed":
    case "graded":
      return "ok";
    case "running":
    case "queued":
    case "planning":
    case "evaluating":
    case "in_progress":
    case "validating":
      return "accent";
    case "waiting_approval":
    case "pending":
    case "paused":
    case "draft":
    case "discovered":
    case "submitted":
      return "warn";
    case "failed":
    case "rejected":
    case "cancelled":
    case "error":
    case "false_positive":
      return "danger";
    default:
      return "neutral";
  }
}

export function severityTone(severity: string): Tone {
  switch (severity) {
    case "critical":
    case "high":
      return "danger";
    case "medium":
      return "warn";
    case "low":
      return "accent";
    default:
      return "neutral";
  }
}
