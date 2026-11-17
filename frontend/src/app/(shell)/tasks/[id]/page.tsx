"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { ErrorNote, Pill, Section, Skeleton, StatusPill } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { formatDate, formatNumber, formatUsd, relativeTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { TaskDetail, TaskRunResponse } from "@/lib/types";

export default function TaskDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const task = useApi<TaskDetail>(`/tasks/${id}`);
  const [run, setRun] = useState<TaskRunResponse | null>(null);
  const [busy, setBusy] = useState<"run" | "cancel" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!task.data) return;
    const live = ["running", "queued", "planning", "waiting_approval"].includes(task.data.status);
    if (!live) {
      if (timer.current) clearInterval(timer.current);
      timer.current = null;
      return;
    }
    timer.current = setInterval(() => task.reload(), 4000);
    return () => {
      if (timer.current) clearInterval(timer.current);
      timer.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.data?.status]);

  async function execute() {
    setBusy("run");
    setError(null);
    try {
      const result = await apiFetch<TaskRunResponse>(`/tasks/${id}/run`, { method: "POST" });
      setRun(result);
      task.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Execution failed");
    } finally {
      setBusy(null);
    }
  }

  async function cancel() {
    setBusy("cancel");
    setError(null);
    try {
      await apiFetch(`/tasks/${id}/cancel`, { method: "POST" });
      task.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Cancel failed");
    } finally {
      setBusy(null);
    }
  }

  if (task.loading && !task.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-6 w-64" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  if (!task.data) {
    return (
      <div className="space-y-3">
        <ErrorNote message={task.error ?? "Task not found"} />
        <Link href="/tasks" className="btn">
          Back to tasks
        </Link>
      </div>
    );
  }

  const data = task.data;
  const terminal = ["completed", "failed", "cancelled"].includes(data.status);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href="/tasks" className="text-[11px] text-[var(--color-faint)] hover:text-[var(--color-ink)]">
            ← Tasks
          </Link>
          <h1 className="mt-1 truncate text-lg font-semibold tracking-tight">{data.title}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <StatusPill status={data.status} />
            <Pill>priority {data.priority}</Pill>
            <span className="text-[11px] text-[var(--color-faint)]">
              created {formatDate(data.created_at)}
            </span>
          </div>
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn btn-primary" disabled={busy !== null || terminal} onClick={execute}>
            {busy === "run" ? "Running…" : "Run now"}
          </button>
          <button type="button" className="btn btn-danger" disabled={busy !== null || terminal} onClick={cancel}>
            {busy === "cancel" ? "Cancelling…" : "Cancel"}
          </button>
          <button type="button" className="btn" onClick={task.reload}>
            Refresh
          </button>
        </div>
      </div>

      <ErrorNote message={error ?? task.error} />

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <Section title="Objective">
            <div className="space-y-3 px-5 py-4">
              <p className="whitespace-pre-wrap text-xs leading-5 text-[var(--color-muted)]">{data.objective}</p>
              {data.description ? (
                <p className="whitespace-pre-wrap border-t border-[var(--color-line)] pt-3 text-[11px] leading-5 text-[var(--color-faint)]">
                  {data.description}
                </p>
              ) : null}
            </div>
          </Section>

          <Section title="Steps" description="Plan steps recorded by the executor">
            {data.steps.length === 0 ? (
              <p className="px-5 py-6 text-xs text-[var(--color-faint)]">
                No steps recorded. Running the task materializes the plan.
              </p>
            ) : (
              <ol className="divide-y divide-[var(--color-line)]">
                {data.steps.map((step) => (
                  <li key={step.id} className="px-5 py-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="mono text-[var(--color-faint)]">{String(step.seq).padStart(2, "0")}</span>
                        <span className="truncate text-xs">{step.title}</span>
                      </div>
                      <StatusPill status={step.status} />
                    </div>
                    {step.description ? (
                      <p className="mt-1 pl-7 text-[11px] text-[var(--color-faint)]">{step.description}</p>
                    ) : null}
                    {step.error ? (
                      <p className="mt-1 pl-7 text-[11px] text-[var(--color-danger)]">{step.error}</p>
                    ) : null}
                  </li>
                ))}
              </ol>
            )}
          </Section>

          <Section title="Event log" description="Append-only history for this task">
            {data.events.length === 0 ? (
              <p className="px-5 py-6 text-xs text-[var(--color-faint)]">No events yet</p>
            ) : (
              <ul className="max-h-96 divide-y divide-[var(--color-line)] overflow-y-auto">
                {data.events.map((event) => (
                  <li key={event.id} className="px-5 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="mono text-[11px] text-[var(--color-accent)]">{event.event_type}</span>
                      <span className="shrink-0 text-[10px] tabular-nums text-[var(--color-faint)]">
                        {relativeTime(event.created_at)}
                      </span>
                    </div>
                    <p className="mt-0.5 text-[11px] text-[var(--color-muted)]">{event.message}</p>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          {run ? (
            <Section title="Run output" description="Synchronous execution result">
              <div className="space-y-3 px-5 py-4">
                <div className="flex flex-wrap gap-2">
                  <StatusPill status={run.status} />
                  <Pill>{run.iterations} iterations</Pill>
                  <Pill>{formatNumber(run.total_tokens)} tokens</Pill>
                  <Pill>{formatUsd(run.cost_usd)}</Pill>
                </div>
                {run.error ? <ErrorNote message={run.error} /> : null}
                <pre className="mono max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--color-canvas)] p-3">
                  {run.content || "(empty response)"}
                </pre>
                {run.tool_invocations.length > 0 ? (
                  <div>
                    <div className="label mb-1.5">Tool calls</div>
                    <ul className="space-y-1">
                      {run.tool_invocations.map((call, index) => (
                        <li key={index} className="mono rounded bg-[var(--color-canvas)] px-2 py-1 text-[11px]">
                          {JSON.stringify(call)}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                {run.pending_approvals.length > 0 ? (
                  <div>
                    <div className="label mb-1.5">Pending approvals</div>
                    <Link href="/approvals" className="btn">
                      Review {run.pending_approvals.length} request(s)
                    </Link>
                  </div>
                ) : null}
              </div>
            </Section>
          ) : null}
        </div>

        <div className="space-y-4">
          <Section title="Accounting">
            <dl className="divide-y divide-[var(--color-line)] text-xs">
              {[
                ["Tokens used", formatNumber(data.used_tokens)],
                ["Token budget", data.budget_tokens ? formatNumber(data.budget_tokens) : "unbounded"],
                ["Iterations", `${data.used_iterations} / ${data.max_iterations}`],
                ["Cost", formatUsd(data.cost_usd)],
                ["Budget", data.budget_usd ? formatUsd(data.budget_usd) : "unbounded"],
                ["Timeout", `${data.timeout_seconds}s`],
              ].map(([key, value]) => (
                <div key={key} className="flex items-center justify-between px-5 py-2">
                  <dt className="text-[var(--color-faint)]">{key}</dt>
                  <dd className="tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          </Section>

          <Section title="Timeline">
            <dl className="divide-y divide-[var(--color-line)] text-xs">
              {[
                ["Created", formatDate(data.created_at)],
                ["Started", formatDate(data.started_at)],
                ["Deadline", formatDate(data.deadline_at)],
                ["Completed", formatDate(data.completed_at)],
                ["Updated", formatDate(data.updated_at)],
              ].map(([key, value]) => (
                <div key={key} className="flex items-center justify-between px-5 py-2">
                  <dt className="text-[var(--color-faint)]">{key}</dt>
                  <dd className="text-[11px] tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          </Section>

          {data.error ? (
            <Section title="Last error">
              <p className="whitespace-pre-wrap px-5 py-4 text-[11px] text-[var(--color-danger)]">{data.error}</p>
            </Section>
          ) : null}

          {Object.keys(data.output).length > 0 ? (
            <Section title="Structured output">
              <pre className="mono max-h-64 overflow-auto whitespace-pre-wrap px-5 py-4">
                {JSON.stringify(data.output, null, 2)}
              </pre>
            </Section>
          ) : null}
        </div>
      </div>
    </div>
  );
}
