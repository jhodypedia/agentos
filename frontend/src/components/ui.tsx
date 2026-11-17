import { cx, statusTone } from "@/lib/format";

export function Pill({
  children,
  tone = "neutral",
  title,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "ok" | "warn" | "danger" | "accent";
  title?: string;
}) {
  const tones: Record<string, string> = {
    neutral: "text-[var(--color-muted)]",
    ok: "text-[var(--color-ok)] border-[color-mix(in_srgb,var(--color-ok)_40%,var(--color-line))]",
    warn: "text-[var(--color-warn)] border-[color-mix(in_srgb,var(--color-warn)_40%,var(--color-line))]",
    danger:
      "text-[var(--color-danger)] border-[color-mix(in_srgb,var(--color-danger)_40%,var(--color-line))]",
    accent:
      "text-[var(--color-accent)] border-[color-mix(in_srgb,var(--color-accent)_40%,var(--color-line))]",
  };
  return (
    <span className={cx("pill", tones[tone])} title={title}>
      {children}
    </span>
  );
}

export function StatusPill({ status }: { status: string }) {
  return <Pill tone={statusTone(status)}>{status.replaceAll("_", " ")}</Pill>;
}

export function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: "ok" | "warn" | "danger";
}) {
  const tones: Record<string, string> = {
    ok: "text-[var(--color-ok)]",
    warn: "text-[var(--color-warn)]",
    danger: "text-[var(--color-danger)]",
  };
  return (
    <div className="card card-pad">
      <div className="label">{label}</div>
      <div className={cx("stat-value mt-1.5", tone ? tones[tone] : "")}>{value}</div>
      {hint ? <div className="mt-1 text-xs text-[var(--color-faint)]">{hint}</div> : null}
    </div>
  );
}

export function Section({
  title,
  action,
  children,
  description,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  description?: string;
}) {
  return (
    <section className="card">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-line)] px-5 py-3.5">
        <div>
          <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
          {description ? (
            <p className="mt-0.5 text-xs text-[var(--color-faint)]">{description}</p>
          ) : null}
        </div>
        {action}
      </header>
      {children}
    </section>
  );
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-14 text-center">
      <p className="text-sm font-medium text-[var(--color-muted)]">{title}</p>
      {hint ? <p className="max-w-md text-xs text-[var(--color-faint)]">{hint}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_40%,var(--color-line))] bg-[color-mix(in_srgb,var(--color-danger)_8%,transparent)] px-3 py-2 text-xs text-[var(--color-danger)]">
      {message}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cx(
        "animate-pulse rounded-md bg-[var(--color-surface-2)]",
        className ?? "h-4 w-full",
      )}
    />
  );
}
