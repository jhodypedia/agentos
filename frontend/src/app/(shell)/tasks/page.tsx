"use client";

import Link from "next/link";
import { useState } from "react";

import { EmptyState, ErrorNote, Section } from "@/components/ui";
import { StatusPill } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { cx, formatNumber, formatUsd, relativeTime, truncate } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { Agent, Project, TaskDetail, TaskPage } from "@/lib/types";

const STATUSES = [
  "pending",
  "planning",
  "queued",
  "running",
  "waiting_approval",
  "paused",
  "completed",
  "failed",
  "cancelled",
] as const;

export default function TasksPage() {
  const [status, setStatus] = useState<string>("");
  const [offset, setOffset] = useState(0);
  const limit = 25;
  const query = `/tasks?limit=${limit}&offset=${offset}${status ? `&status=${status}` : ""}`;
  const tasks = useApi<TaskPage>(query, [status, offset]);
  const agents = useApi<Agent[]>("/agents");
  const projects = useApi<Project[]>("/projects");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ title: "", objective: "", description: "", agent: "", project: "", budget: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiFetch<TaskDetail>("/tasks", {
        method: "POST",
        body: JSON.stringify({
          title: form.title,
          objective: form.objective,
          description: form.description,
          assigned_agent_id: form.agent || null,
          project_id: form.project || null,
          budget_tokens: form.budget ? Number(form.budget) : null,
        }),
      });
      setForm({ title: "", objective: "", description: "", agent: "", project: "", budget: "" });
      setShowForm(false);
      tasks.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create the task");
    } finally {
      setBusy(false);
    }
  }

  const pages = tasks.data ? Math.ceil(tasks.data.total / limit) : 0;
  const current = Math.floor(offset / limit) + 1;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Task center</h1>
          <p className="mt-0.5 text-xs text-[var(--color-faint)]">
            Every objective dispatched to the orchestrator, with budget and cost accounting.
          </p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setShowForm((value) => !value)}>
          {showForm ? "Cancel" : "New task"}
        </button>
      </header>

      {showForm ? (
        <Section title="Create task">
          <form onSubmit={create} className="grid gap-3 px-5 py-4">
            <div className="grid gap-3 md:grid-cols-2">
              <label className="space-y-1.5">
                <span className="label">Title</span>
                <input
                  className="input"
                  required
                  value={form.title}
                  onChange={(event) => setForm({ ...form, title: event.target.value })}
                  placeholder="Audit the auth module"
                />
              </label>
              <label className="space-y-1.5">
                <span className="label">Agent</span>
                <select
                  className="input"
                  value={form.agent}
                  onChange={(event) => setForm({ ...form, agent: event.target.value })}
                >
                  <option value="">Unassigned (pick at run time)</option>
                  {(agents.data ?? []).map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label className="space-y-1.5">
              <span className="label">Objective</span>
              <textarea
                className="input min-h-24"
                required
                value={form.objective}
                onChange={(event) => setForm({ ...form, objective: event.target.value })}
                placeholder="Describe what the agent must accomplish and how success is judged."
              />
            </label>
            <div className="grid gap-3 md:grid-cols-3">
              <label className="space-y-1.5">
                <span className="label">Project</span>
                <select
                  className="input"
                  value={form.project}
                  onChange={(event) => setForm({ ...form, project: event.target.value })}
                >
                  <option value="">None</option>
                  {(projects.data ?? []).map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="space-y-1.5">
                <span className="label">Token budget</span>
                <input
                  className="input"
                  type="number"
                  min="1"
                  value={form.budget}
                  onChange={(event) => setForm({ ...form, budget: event.target.value })}
                  placeholder="default"
                />
              </label>
              <label className="space-y-1.5">
                <span className="label">Notes</span>
                <input
                  className="input"
                  value={form.description}
                  onChange={(event) => setForm({ ...form, description: event.target.value })}
                  placeholder="Optional context"
                />
              </label>
            </div>
            <div>
              <ErrorNote message={error} />
              <button type="submit" className="btn btn-primary mt-2" disabled={busy}>
                {busy ? "Creating…" : "Create task"}
              </button>
            </div>
          </form>
        </Section>
      ) : null}

      <div className="flex flex-wrap gap-1.5">
        <button
          type="button"
          className={cx("btn px-2.5 py-1 text-[11px]", status === "" ? "border-[var(--color-accent)] text-[var(--color-accent)]" : "")}
          onClick={() => {
            setStatus("");
            setOffset(0);
          }}
        >
          All
        </button>
        {STATUSES.map((name) => (
          <button
            key={name}
            type="button"
            className={cx(
              "btn px-2.5 py-1 text-[11px] capitalize",
              status === name ? "border-[var(--color-accent)] text-[var(--color-accent)]" : "",
            )}
            onClick={() => {
              setStatus(name);
              setOffset(0);
            }}
          >
            {name.replaceAll("_", " ")}
          </button>
        ))}
      </div>

      <ErrorNote message={tasks.error} />

      <div className="card overflow-x-auto">
        {tasks.data && tasks.data.items.length > 0 ? (
          <table className="data">
            <thead>
              <tr>
                <th>Title</th>
                <th>Status</th>
                <th>Tokens</th>
                <th>Cost</th>
                <th>Iterations</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {tasks.data.items.map((task) => (
                <tr key={task.id}>
                  <td className="max-w-md">
                    <Link href={`/tasks/${task.id}`} className="hover:text-[var(--color-accent)]">
                      {truncate(task.title, 80)}
                    </Link>
                    <div className="truncate text-[11px] text-[var(--color-faint)]">{truncate(task.objective, 100)}</div>
                  </td>
                  <td>
                    <StatusPill status={task.status} />
                  </td>
                  <td className="tabular-nums">
                    {formatNumber(task.used_tokens)}
                    {task.budget_tokens ? (
                      <span className="text-[var(--color-faint)]"> / {formatNumber(task.budget_tokens)}</span>
                    ) : null}
                  </td>
                  <td className="tabular-nums">{formatUsd(task.cost_usd)}</td>
                  <td className="tabular-nums">
                    {task.used_iterations}
                    <span className="text-[var(--color-faint)]"> / {task.max_iterations}</span>
                  </td>
                  <td className="whitespace-nowrap text-[var(--color-faint)]">{relativeTime(task.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState
            title={tasks.loading ? "Loading tasks…" : "No tasks match this filter"}
            hint="Create a task to dispatch an objective to an agent."
          />
        )}
      </div>

      {pages > 1 ? (
        <div className="flex items-center justify-between text-[11px] text-[var(--color-faint)]">
          <span>
            Page {current} of {pages} · {tasks.data?.total ?? 0} tasks
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              className="btn px-2 py-1 text-[11px]"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              Previous
            </button>
            <button
              type="button"
              className="btn px-2 py-1 text-[11px]"
              disabled={!tasks.data || offset + limit >= tasks.data.total}
              onClick={() => setOffset(offset + limit)}
            >
              Next
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
