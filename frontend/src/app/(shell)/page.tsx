"use client";

import Link from "next/link";
import { useMemo } from "react";

import { Breakdown, UsageChart } from "@/components/charts";
import { ErrorNote, Section, Skeleton, Stat, StatusPill } from "@/components/ui";
import { formatNumber, formatUsd, relativeTime, truncate } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { useEventStream } from "@/lib/use-event-stream";
import type { DashboardSummary, TaskPage, UsageSeries } from "@/lib/types";

const STATUS_COLORS: Record<string, string> = {
  completed: "var(--color-ok)",
  running: "var(--color-accent)",
  waiting_approval: "var(--color-warn)",
  failed: "var(--color-danger)",
  pending: "#7c8493",
  cancelled: "#5b616c",
};

export default function DashboardPage() {
  const summary = useApi<DashboardSummary>("/dashboard/summary");
  const usage = useApi<UsageSeries>("/dashboard/usage?days=14");
  const recent = useApi<TaskPage>("/tasks?limit=8");
  const { events, status: streamStatus } = useEventStream({ limit: 40 });

  const healthEntries = useMemo(() => Object.entries(summary.data?.health ?? {}), [summary.data]);

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Dashboard</h1>
          <p className="mt-0.5 text-xs text-[var(--color-faint)]">
            Platform health, throughput and spend across every agent.
          </p>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-[var(--color-faint)]">
          <span className="flex items-center gap-1.5">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                streamStatus === "live" ? "live-dot bg-[var(--color-ok)]" : "bg-[var(--color-warn)]"
              }`}
            />
            {streamStatus}
          </span>
          <button type="button" className="btn px-2 py-1 text-[11px]" onClick={summary.reload}>
            Refresh
          </button>
        </div>
      </header>

      <ErrorNote message={summary.error} />

      {summary.loading && !summary.data ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((index) => (
            <div key={index} className="card card-pad">
              <Skeleton className="h-2.5 w-20" />
              <Skeleton className="mt-3 h-7 w-16" />
            </div>
          ))}
        </div>
      ) : summary.data ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Stat label="Active agents" value={summary.data.agents_by_status.active ?? 0} hint={`${summary.data.agents_total} total`} />
            <Stat
              label="Tasks running"
              value={summary.data.tasks_by_status.running ?? 0}
              hint={`${summary.data.tasks_total} total`}
            />
            <Stat
              label="Pending approvals"
              value={summary.data.approvals_pending}
              tone={summary.data.approvals_pending > 0 ? "warn" : undefined}
              hint={summary.data.approvals_pending > 0 ? "Waiting on a human" : "Queue clear"}
            />
            <Stat
              label="Spend to date"
              value={formatUsd(summary.data.total_cost_usd)}
              hint={`${formatNumber(summary.data.total_tokens)} tokens`}
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-3">
            <div className="xl:col-span-2">
              <Section
                title="Token usage"
                description="Daily totals over the last 14 days"
                action={<span className="text-[11px] text-[var(--color-faint)]">{formatUsd(usage.data?.total_cost_usd ?? 0)}</span>}
              >
                {usage.loading && !usage.data ? (
                  <div className="px-5 py-6">
                    <Skeleton className="h-36 w-full" />
                  </div>
                ) : usage.error ? (
                  <div className="px-5 py-4">
                    <ErrorNote message={usage.error} />
                  </div>
                ) : usage.data ? (
                  <UsageChart series={usage.data} metric="tokens" />
                ) : null}
              </Section>
            </div>

            <Section title="Service health" description="Live dependency probes">
              <ul className="divide-y divide-[var(--color-line)]">
                {healthEntries.map(([name, state]) => (
                  <li key={name} className="flex items-center justify-between px-5 py-2.5">
                    <span className="text-xs capitalize">{name}</span>
                    <StatusPill status={state} />
                  </li>
                ))}
              </ul>
              <div className="border-t border-[var(--color-line)] px-5 py-3 text-[11px] text-[var(--color-faint)]">
                <div className="flex justify-between">
                  <span>Open competitions</span>
                  <span className="tabular-nums">{summary.data.open_competitions}</span>
                </div>
                <div className="mt-1 flex justify-between">
                  <span>Telegram bot</span>
                  <span>{summary.data.limits.telegram_configured ? "configured" : "not configured"}</span>
                </div>
                <div className="mt-1 flex justify-between">
                  <span>make.fun API</span>
                  <span>{summary.data.limits.makefun_configured ? "configured" : "not configured"}</span>
                </div>
              </div>
            </Section>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Section title="Task status mix">
              <Breakdown data={summary.data.tasks_by_status} tones={STATUS_COLORS} />
            </Section>
            <Section title="Findings by severity">
              <Breakdown
                data={summary.data.findings_by_severity}
                tones={{
                  critical: "#ef4444",
                  high: "#f87171",
                  medium: "var(--color-warn)",
                  low: "var(--color-accent)",
                  info: "#5b616c",
                }}
              />
            </Section>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Section title="Recent audit activity" description="Operator actions recorded server-side">
              {summary.data.activity.length === 0 ? (
                <p className="px-5 py-6 text-xs text-[var(--color-faint)]">No audited actions yet</p>
              ) : (
                <ul className="divide-y divide-[var(--color-line)]">
                  {summary.data.activity.map((item) => (
                    <li key={item.id} className="flex items-center justify-between gap-3 px-5 py-2.5">
                      <div className="min-w-0">
                        <div className="truncate text-xs">{item.action}</div>
                        <div className="truncate text-[11px] text-[var(--color-faint)]">
                          {item.resource_type}
                        </div>
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        {item.success ? null : <StatusPill status="failed" />}
                        <span className="text-[11px] tabular-nums text-[var(--color-faint)]">
                          {relativeTime(item.created_at)}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            <Section title="Live events" description="Redis pub/sub stream from running workers">
              {events.length === 0 ? (
                <p className="px-5 py-6 text-xs text-[var(--color-faint)]">
                  {streamStatus === "live" ? "Stream connected, nothing since page load" : "Waiting for the stream…"}
                </p>
              ) : (
                <ul className="max-h-72 divide-y divide-[var(--color-line)] overflow-y-auto">
                  {events.map((event) => (
                    <li key={event.id} className="px-5 py-2">
                      <div className="flex items-center justify-between gap-2">
                        <span className="mono text-[var(--color-accent)]">{event.type}</span>
                        <span className="shrink-0 text-[11px] tabular-nums text-[var(--color-faint)]">
                          {relativeTime(event.occurred_at)}
                        </span>
                      </div>
                      <div className="truncate text-[11px] text-[var(--color-faint)]">
                        {truncate(event.source, 80)}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </Section>
          </div>
        </>
      ) : null}

      <Section
        title="Latest tasks"
        action={
          <Link href="/tasks" className="text-[11px] text-[var(--color-accent)] hover:underline">
            View all
          </Link>
        }
      >
        {recent.error ? (
          <div className="px-5 py-4">
            <ErrorNote message={recent.error} />
          </div>
        ) : recent.data && recent.data.items.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="data">
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Status</th>
                  <th>Tokens</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {recent.data.items.map((task) => (
                  <tr key={task.id}>
                    <td>
                      <Link href={`/tasks/${task.id}`} className="hover:text-[var(--color-accent)]">
                        {truncate(task.title || task.objective, 70)}
                      </Link>
                    </td>
                    <td>
                      <StatusPill status={task.status} />
                    </td>
                    <td className="tabular-nums">{formatNumber(task.used_tokens)}</td>
                    <td className="text-[var(--color-faint)]">{relativeTime(task.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="px-5 py-6 text-xs text-[var(--color-faint)]">No tasks created yet</p>
        )}
      </Section>
    </div>
  );
}
