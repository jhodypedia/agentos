import type { UsageSeries } from "@/lib/types";

/** Dependency-free SVG area chart for a daily usage series. */
export function UsageChart({ series, metric }: { series: UsageSeries; metric: "tokens" | "cost_usd" | "requests" }) {
  const points = series.points;
  if (points.length === 0) {
    return (
      <div className="grid h-40 place-items-center text-xs text-[var(--color-faint)]">
        No usage recorded in this window
      </div>
    );
  }

  const values = points.map((point) =>
    metric === "tokens" ? point.tokens : metric === "requests" ? point.requests : Number(point.cost_usd),
  );
  const max = Math.max(...values, 1);
  const width = 720;
  const height = 160;
  const padX = 6;
  const padY = 10;
  const step = points.length > 1 ? (width - padX * 2) / (points.length - 1) : 0;
  const y = (value: number) => height - padY - (value / max) * (height - padY * 2);
  const x = (index: number) => padX + index * step;

  const line = values.map((value, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(2)},${y(value).toFixed(2)}`).join(" ");
  const area = `${line} L${x(values.length - 1).toFixed(2)},${height - padY} L${x(0).toFixed(2)},${height - padY} Z`;

  return (
    <div className="px-2 pb-1">
      <svg viewBox={`0 0 ${width} ${height}`} className="h-40 w-full" role="img" aria-label={`Daily ${metric} usage`}>
        <defs>
          <linearGradient id={`fill-${metric}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-accent)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--color-accent)" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((ratio) => (
          <line
            key={ratio}
            x1={padX}
            x2={width - padX}
            y1={padY + ratio * (height - padY * 2)}
            y2={padY + ratio * (height - padY * 2)}
            stroke="var(--color-line)"
            strokeDasharray="3 4"
          />
        ))}
        <path d={area} fill={`url(#fill-${metric})`} />
        <path d={line} fill="none" stroke="var(--color-accent)" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
        {values.map((value, index) => (
          <circle key={points[index]?.day ?? index} cx={x(index)} cy={y(value)} r={values.length > 30 ? 0 : 2.2} fill="var(--color-accent)">
            <title>{`${points[index]?.day ?? ""}: ${value.toLocaleString()}`}</title>
          </circle>
        ))}
      </svg>
      <div className="flex justify-between px-1 text-[10px] text-[var(--color-faint)]">
        <span>{points[0]?.day}</span>
        <span>peak {max.toLocaleString()}</span>
        <span>{points[points.length - 1]?.day}</span>
      </div>
    </div>
  );
}

/** Horizontal stacked bars, used for status breakdowns. */
export function Breakdown({
  data,
  tones,
}: {
  data: Record<string, number>;
  tones?: Record<string, string>;
}) {
  const entries = Object.entries(data).filter(([, value]) => value !== 0);
  const total = entries.reduce((sum, [, value]) => sum + value, 0);
  if (total === 0) {
    return <p className="px-5 py-6 text-xs text-[var(--color-faint)]">Nothing recorded yet</p>;
  }
  return (
    <div className="space-y-2 px-5 py-4">
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-[var(--color-surface-2)]">
        {entries.map(([key, value]) => (
          <div
            key={key}
            className="h-full"
            style={{
              width: `${(value / total) * 100}%`,
              background: tones?.[key] ?? "var(--color-accent)",
            }}
            title={`${key}: ${value}`}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {entries.map(([key, value]) => (
          <div key={key} className="flex items-center gap-1.5 text-[11px] text-[var(--color-muted)]">
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ background: tones?.[key] ?? "var(--color-accent)" }}
            />
            <span className="capitalize">{key.replaceAll("_", " ")}</span>
            <span className="tabular-nums text-[var(--color-faint)]">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
