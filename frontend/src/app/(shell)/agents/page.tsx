"use client";

import Link from "next/link";
import { useState } from "react";

import { EmptyState, ErrorNote, Section, StatusPill } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cx, formatDate, truncate } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { Agent } from "@/lib/types";

const EMPTY = {
  slug: "",
  name: "",
  description: "",
  system_prompt: "",
  model_name: "",
  temperature: "0.2",
  max_iterations: "25",
  token_budget: "200000",
};

export default function AgentsPage() {
  const agents = useApi<Agent[]>("/agents");
  const { can } = useAuth();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiFetch<Agent>("/agents", {
        method: "POST",
        body: JSON.stringify({
          slug: form.slug,
          name: form.name,
          description: form.description,
          system_prompt: form.system_prompt,
          model_name: form.model_name || null,
          temperature: Number(form.temperature),
          max_iterations: Number(form.max_iterations),
          token_budget: Number(form.token_budget),
        }),
      });
      setForm(EMPTY);
      setShowForm(false);
      agents.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create the agent");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Agent manager</h1>
          <p className="mt-0.5 text-xs text-[var(--color-faint)]">
            Configure the personas, budgets and permissions the orchestrator can dispatch.
          </p>
        </div>
        {can("admin") ? (
          <button type="button" className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
            {showForm ? "Cancel" : "New agent"}
          </button>
        ) : null}
      </header>

      <ErrorNote message={agents.error} />

      {showForm ? (
        <Section title="Create agent">
          <form onSubmit={create} className="grid gap-3 px-5 py-4 md:grid-cols-2">
            <label className="space-y-1.5">
              <span className="label">Slug</span>
              <input
                className="input"
                required
                pattern="[a-z0-9][a-z0-9_-]*"
                value={form.slug}
                onChange={(event) => setForm({ ...form, slug: event.target.value })}
                placeholder="security-scout"
              />
            </label>
            <label className="space-y-1.5">
              <span className="label">Name</span>
              <input
                className="input"
                required
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                placeholder="Security Scout"
              />
            </label>
            <label className="space-y-1.5 md:col-span-2">
              <span className="label">Description</span>
              <input
                className="input"
                value={form.description}
                onChange={(event) => setForm({ ...form, description: event.target.value })}
              />
            </label>
            <label className="space-y-1.5 md:col-span-2">
              <span className="label">System prompt</span>
              <textarea
                className="input min-h-24"
                value={form.system_prompt}
                onChange={(event) => setForm({ ...form, system_prompt: event.target.value })}
                placeholder="You are a careful security reviewer…"
              />
            </label>
            <label className="space-y-1.5">
              <span className="label">Model</span>
              <input
                className="input"
                value={form.model_name}
                onChange={(event) => setForm({ ...form, model_name: event.target.value })}
                placeholder="gpt-6-astra"
              />
            </label>
            <label className="space-y-1.5">
              <span className="label">Temperature</span>
              <input
                className="input"
                type="number"
                step="0.1"
                min="0"
                max="2"
                value={form.temperature}
                onChange={(event) => setForm({ ...form, temperature: event.target.value })}
              />
            </label>
            <label className="space-y-1.5">
              <span className="label">Max iterations</span>
              <input
                className="input"
                type="number"
                min="1"
                value={form.max_iterations}
                onChange={(event) => setForm({ ...form, max_iterations: event.target.value })}
              />
            </label>
            <label className="space-y-1.5">
              <span className="label">Token budget</span>
              <input
                className="input"
                type="number"
                min="1"
                value={form.token_budget}
                onChange={(event) => setForm({ ...form, token_budget: event.target.value })}
              />
            </label>
            <div className="md:col-span-2">
              <ErrorNote message={error} />
              <button type="submit" className="btn btn-primary mt-2" disabled={busy}>
                {busy ? "Creating…" : "Create agent"}
              </button>
            </div>
          </form>
        </Section>
      ) : null}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {agents.loading && !agents.data
          ? [0, 1, 2].map((index) => <div key={index} className="card h-32 animate-pulse" />)
          : null}
        {agents.data?.map((agent) => (
          <Link
            key={agent.id}
            href={`/agents/${agent.id}`}
            className="card card-pad transition-colors hover:border-[var(--color-line-strong)]"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{agent.name}</div>
                <div className="mono truncate text-[11px] text-[var(--color-faint)]">{agent.slug}</div>
              </div>
              <StatusPill status={agent.status} />
            </div>
            <p className="mt-2 h-8 text-[11px] leading-4 text-[var(--color-muted)]">
              {truncate(agent.description || "No description", 110)}
            </p>
            <div className="mt-3 flex items-center justify-between border-t border-[var(--color-line)] pt-2.5 text-[11px] text-[var(--color-faint)]">
              <span className={cx("mono", agent.model_name ? "" : "italic")}>
                {agent.model_name ?? "unassigned model"}
              </span>
              <span>v{agent.version}</span>
            </div>
            <div className="mt-1.5 text-[10px] text-[var(--color-faint)]">
              Updated {formatDate(agent.updated_at)}
            </div>
          </Link>
        ))}
      </div>

      {!agents.loading && agents.data?.length === 0 ? (
        <div className="card">
          <EmptyState
            title="No agents yet"
            hint={
              can("admin")
                ? "Create the first agent to give the orchestrator a persona to dispatch."
                : "An administrator must create agents before they appear here."
            }
          />
        </div>
      ) : null}
    </div>
  );
}
