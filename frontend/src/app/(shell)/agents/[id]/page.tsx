"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ErrorNote, Pill, Section, Skeleton, StatusPill } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate, formatNumber } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { AgentDetail, Skill, Tool } from "@/lib/types";

const STATUSES = ["active", "paused", "draft", "archived"] as const;

export default function AgentDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const router = useRouter();
  const { can } = useAuth();
  const agent = useApi<AgentDetail>(`/agents/${id}`);
  const tools = useApi<Tool[]>("/tools");
  const skills = useApi<Skill[]>("/skills");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState({ name: "", description: "", system_prompt: "", model_name: "", temperature: "" });

  useEffect(() => {
    if (!agent.data) return;
    setDraft({
      name: agent.data.name,
      description: agent.data.description,
      system_prompt: agent.data.system_prompt,
      model_name: agent.data.model_name ?? "",
      temperature: String(agent.data.temperature),
    });
  }, [agent.data]);

  async function patch(body: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    try {
      await apiFetch<AgentDetail>(`/agents/${id}`, { method: "PATCH", body: JSON.stringify(body) });
      agent.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Update failed");
    } finally {
      setBusy(false);
    }
  }

  async function toggleTool(toolSlug: string, enabled: boolean, assigned: boolean) {
    setBusy(true);
    setError(null);
    try {
      if (assigned && !enabled) return;
      await apiFetch(`/agents/${id}/tools`, {
        method: "POST",
        body: JSON.stringify({ tool_slug: toolSlug, enabled, config: {} }),
      });
      agent.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not assign the tool");
    } finally {
      setBusy(false);
    }
  }

  async function toggleSkill(skillId: string, enabled: boolean) {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/agents/${id}/skills`, {
        method: "POST",
        body: JSON.stringify({ skill_id: skillId, enabled }),
      });
      agent.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not assign the skill");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm("Delete this agent? Tasks that reference it keep their history.")) return;
    setBusy(true);
    try {
      await apiFetch(`/agents/${id}`, { method: "DELETE" });
      router.replace("/agents");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Delete failed");
      setBusy(false);
    }
  }

  if (agent.loading && !agent.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (!agent.data) {
    return (
      <div className="space-y-3">
        <ErrorNote message={agent.error ?? "Agent not found"} />
        <Link href="/agents" className="btn">
          Back to agents
        </Link>
      </div>
    );
  }

  const data = agent.data;
  const toolById = new Map((tools.data ?? []).map((tool) => [tool.id, tool]));
  const assignedSkillIds = new Set(data.skills.map((entry) => entry.skill_id));

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link href="/agents" className="text-[11px] text-[var(--color-faint)] hover:text-[var(--color-ink)]">
            ← Agents
          </Link>
          <h1 className="mt-1 text-lg font-semibold tracking-tight">{data.name}</h1>
          <div className="mt-1 flex items-center gap-2">
            <span className="mono text-[11px] text-[var(--color-faint)]">{data.slug}</span>
            <StatusPill status={data.status} />
            {data.is_system ? <Pill>system</Pill> : null}
            <Pill>v{data.version}</Pill>
          </div>
        </div>
        {can("admin") ? (
          <div className="flex gap-2">
            {STATUSES.filter((status) => status !== data.status).map((status) => (
              <button
                key={status}
                type="button"
                className="btn px-2.5 py-1 text-[11px] capitalize"
                disabled={busy}
                onClick={() => patch({ status })}
              >
                {status}
              </button>
            ))}
            <button type="button" className="btn btn-danger px-2.5 py-1 text-[11px]" disabled={busy || data.is_system} onClick={remove}>
              Delete
            </button>
          </div>
        ) : null}
      </div>

      <ErrorNote message={error} />

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <Section title="Definition">
            {can("admin") ? (
              <form
                className="grid gap-3 px-5 py-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  void patch({
                    name: draft.name,
                    description: draft.description,
                    system_prompt: draft.system_prompt,
                    model_name: draft.model_name || null,
                    temperature: Number(draft.temperature),
                  });
                }}
              >
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="space-y-1.5">
                    <span className="label">Name</span>
                    <input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
                  </label>
                  <label className="space-y-1.5">
                    <span className="label">Model</span>
                    <input
                      className="input"
                      value={draft.model_name}
                      onChange={(e) => setDraft({ ...draft, model_name: e.target.value })}
                      placeholder="unassigned"
                    />
                  </label>
                </div>
                <label className="space-y-1.5">
                  <span className="label">Description</span>
                  <input
                    className="input"
                    value={draft.description}
                    onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="label">System prompt</span>
                  <textarea
                    className="input min-h-40 font-mono text-[12px] leading-5"
                    value={draft.system_prompt}
                    onChange={(e) => setDraft({ ...draft, system_prompt: e.target.value })}
                  />
                </label>
                <div className="flex items-center gap-2">
                  <button type="submit" className="btn btn-primary" disabled={busy}>
                    Save changes
                  </button>
                  <span className="text-[11px] text-[var(--color-faint)]">
                    Updated {formatDate(data.updated_at)}
                  </span>
                </div>
              </form>
            ) : (
              <div className="space-y-3 px-5 py-4">
                <p className="text-xs text-[var(--color-muted)]">{data.description || "No description"}</p>
                <pre className="mono max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--color-canvas)] p-3">
                  {data.system_prompt || "No system prompt"}
                </pre>
              </div>
            )}
          </Section>

          <Section title="Tools" description="Capabilities the executor may call on this agent's behalf">
            <ul className="divide-y divide-[var(--color-line)]">
              {(tools.data ?? []).map((tool) => {
                const assignment = data.tools.find((entry) => entry.tool_id === tool.id);
                return (
                  <li key={tool.id} className="flex items-center justify-between gap-3 px-5 py-2.5">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="mono text-xs">{tool.slug}</span>
                        <Pill tone={tool.risk_level === "high" ? "danger" : tool.risk_level === "medium" ? "warn" : "neutral"}>
                          {tool.risk_level}
                        </Pill>
                      </div>
                      <div className="truncate text-[11px] text-[var(--color-faint)]">{tool.description}</div>
                    </div>
                    {can("admin") ? (
                      <button
                        type="button"
                        className={assignment?.enabled ? "btn btn-danger px-2 py-0.5 text-[11px]" : "btn px-2 py-0.5 text-[11px]"}
                        disabled={busy}
                        onClick={() => toggleTool(tool.slug, !assignment?.enabled, Boolean(assignment))}
                      >
                        {assignment?.enabled ? "Revoke" : "Assign"}
                      </button>
                    ) : (
                      <Pill tone={assignment?.enabled ? "ok" : "neutral"}>
                        {assignment?.enabled ? "enabled" : "not assigned"}
                      </Pill>
                    )}
                  </li>
                );
              })}
              {tools.data?.length === 0 ? (
                <li className="px-5 py-6 text-xs text-[var(--color-faint)]">
                  No tools registered. The seed step installs the builtin registry.
                </li>
              ) : null}
            </ul>
          </Section>
        </div>

        <div className="space-y-4">
          <Section title="Limits">
            <dl className="divide-y divide-[var(--color-line)] text-xs">
              {[
                ["Temperature", String(data.temperature)],
                ["Top-p", String(data.top_p)],
                ["Max output tokens", formatNumber(data.max_output_tokens)],
                ["Max iterations", formatNumber(data.max_iterations)],
                ["Token budget", formatNumber(data.token_budget)],
                ["Timeout", `${data.timeout_seconds}s`],
                ["Created", formatDate(data.created_at)],
              ].map(([key, value]) => (
                <div key={key} className="flex items-center justify-between px-5 py-2">
                  <dt className="text-[var(--color-faint)]">{key}</dt>
                  <dd className="tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          </Section>

          <Section title="Skills" description="Instruction packs attached to this agent">
            <ul className="divide-y divide-[var(--color-line)]">
              {(skills.data ?? []).map((skill) => {
                const active = assignedSkillIds.has(skill.id);
                return (
                  <li key={skill.id} className="flex items-center justify-between gap-2 px-5 py-2">
                    <div className="min-w-0">
                      <div className="truncate text-xs">{skill.name}</div>
                      <div className="mono truncate text-[10px] text-[var(--color-faint)]">{skill.slug}</div>
                    </div>
                    {can("admin") ? (
                      <button
                        type="button"
                        className="btn px-2 py-0.5 text-[11px]"
                        disabled={busy}
                        onClick={() => toggleSkill(skill.id, !active)}
                      >
                        {active ? "Remove" : "Attach"}
                      </button>
                    ) : (
                      <Pill tone={active ? "ok" : "neutral"}>{active ? "attached" : "—"}</Pill>
                    )}
                  </li>
                );
              })}
              {skills.data?.length === 0 ? (
                <li className="px-5 py-5 text-xs text-[var(--color-faint)]">No skills installed</li>
              ) : null}
            </ul>
          </Section>

          <Section title="Tool assignments">
            {data.tools.length === 0 ? (
              <p className="px-5 py-5 text-xs text-[var(--color-faint)]">No tools assigned</p>
            ) : (
              <ul className="divide-y divide-[var(--color-line)]">
                {data.tools.map((entry) => (
                  <li key={entry.id} className="flex items-center justify-between px-5 py-2 text-xs">
                    <span className="mono">{toolById.get(entry.tool_id)?.slug ?? entry.tool_id.slice(0, 8)}</span>
                    <Pill tone={entry.enabled ? "ok" : "neutral"}>{entry.enabled ? "enabled" : "disabled"}</Pill>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </div>
      </div>
    </div>
  );
}
