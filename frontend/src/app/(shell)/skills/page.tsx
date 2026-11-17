"use client";

import { useState } from "react";

import { EmptyState, ErrorNote, Pill, Section, Skeleton } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate, truncate } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { Skill, SkillDetail } from "@/lib/types";

export default function SkillsPage() {
  const skills = useApi<Skill[]>("/skills?limit=200");
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useApi<SkillDetail>(selected ? `/skills/${selected}` : null, [selected]);
  const { can } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [draft, setDraft] = useState({ slug: "", name: "", description: "", body: "" });

  async function toggle(skill: Skill) {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/skills/${skill.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !skill.enabled }),
      });
      skills.reload();
      if (selected === skill.id) detail.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Update failed");
    } finally {
      setBusy(false);
    }
  }

  async function sync() {
    setBusy(true);
    setError(null);
    try {
      await apiFetch("/skills/sync", { method: "POST" });
      skills.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sync failed");
    } finally {
      setBusy(false);
    }
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await apiFetch<SkillDetail>("/skills", {
        method: "POST",
        body: JSON.stringify({
          slug: draft.slug,
          name: draft.name,
          description: draft.description,
          body: draft.body,
        }),
      });
      setDraft({ slug: "", name: "", description: "", body: "" });
      setShowCreate(false);
      skills.reload();
      setSelected(created.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create the skill");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Skill manager</h1>
          <p className="mt-0.5 text-xs text-[var(--color-faint)]">
            Instruction packs loaded from the skills directory and attached to agents.
          </p>
        </div>
        {can("admin") ? (
          <div className="flex gap-2">
            <button type="button" className="btn" onClick={sync} disabled={busy}>
              Rescan directory
            </button>
            <button type="button" className="btn btn-primary" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? "Cancel" : "New skill"}
            </button>
          </div>
        ) : null}
      </header>

      <ErrorNote message={error ?? skills.error} />

      {showCreate ? (
        <Section title="Create skill">
          <form onSubmit={create} className="grid gap-3 px-5 py-4 md:grid-cols-2">
            <label className="space-y-1.5">
              <span className="label">Slug</span>
              <input
                className="input"
                required
                pattern="[a-z0-9][a-z0-9-]*"
                value={draft.slug}
                onChange={(event) => setDraft({ ...draft, slug: event.target.value })}
              />
            </label>
            <label className="space-y-1.5">
              <span className="label">Name</span>
              <input
                className="input"
                required
                value={draft.name}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              />
            </label>
            <label className="space-y-1.5 md:col-span-2">
              <span className="label">Description</span>
              <input
                className="input"
                value={draft.description}
                onChange={(event) => setDraft({ ...draft, description: event.target.value })}
              />
            </label>
            <label className="space-y-1.5 md:col-span-2">
              <span className="label">SKILL.md body</span>
              <textarea
                className="input min-h-32 font-mono text-[12px] leading-5"
                required
                value={draft.body}
                onChange={(event) => setDraft({ ...draft, body: event.target.value })}
                placeholder={"# Instructions\n\n1. …"}
              />
            </label>
            <div className="md:col-span-2">
              <button type="submit" className="btn btn-primary" disabled={busy}>
                {busy ? "Creating…" : "Create skill"}
              </button>
            </div>
          </form>
        </Section>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-5">
        <div className="card xl:col-span-2">
          {skills.loading && !skills.data ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2, 3].map((index) => (
                <Skeleton key={index} className="h-10 w-full" />
              ))}
            </div>
          ) : skills.data && skills.data.length > 0 ? (
            <ul className="divide-y divide-[var(--color-line)]">
              {skills.data.map((skill) => (
                <li key={skill.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(skill.id)}
                    className={`flex w-full items-start justify-between gap-2 px-4 py-2.5 text-left transition-colors hover:bg-[var(--color-surface-2)] ${
                      selected === skill.id ? "bg-[var(--color-surface-2)]" : ""
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate text-xs font-medium">{skill.name}</span>
                        {skill.is_system ? <Pill>system</Pill> : null}
                      </div>
                      <div className="mono truncate text-[10px] text-[var(--color-faint)]">
                        {skill.slug} · v{skill.version} · {skill.source}
                      </div>
                      <div className="truncate text-[10px] text-[var(--color-faint)]">
                        {truncate(skill.description || "No description", 80)}
                      </div>
                    </div>
                    <Pill tone={skill.enabled ? "ok" : "neutral"}>{skill.enabled ? "on" : "off"}</Pill>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              title={skills.loading ? "Loading skills…" : "No skills installed"}
              hint="Drop a SKILL.md folder under the skills root and rescan, or create one here."
            />
          )}
        </div>

        <div className="xl:col-span-3">
          {selected && detail.data ? (
            <Section
              title={detail.data.name}
              description={`${detail.data.slug} · v${detail.data.version} · ${detail.data.files.length} file(s)`}
              action={
                can("admin") ? (
                  <button type="button" className="btn px-2 py-1 text-[11px]" disabled={busy} onClick={() => toggle(detail.data as Skill)}>
                    {detail.data.enabled ? "Disable" : "Enable"}
                  </button>
                ) : null
              }
            >
              <div className="space-y-3 px-5 py-4">
                <div className="flex flex-wrap gap-2 text-[11px] text-[var(--color-faint)]">
                  <span>Source {detail.data.source}</span>
                  <span>·</span>
                  <span>Updated {formatDate(detail.data.updated_at)}</span>
                  <span>·</span>
                  <span className="mono">{detail.data.content_hash.slice(0, 12)}</span>
                </div>
                {detail.data.description ? (
                  <p className="text-xs text-[var(--color-muted)]">{detail.data.description}</p>
                ) : null}
                {Object.keys(detail.data.frontmatter).length > 0 ? (
                  <pre className="mono overflow-auto rounded-lg bg-[var(--color-canvas)] p-3 text-[11px]">
                    {JSON.stringify(detail.data.frontmatter, null, 2)}
                  </pre>
                ) : null}
                <pre className="mono max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--color-canvas)] p-3 text-[12px] leading-5">
                  {detail.data.body}
                </pre>
                {detail.data.assigned_agent_ids.length > 0 ? (
                  <div className="text-[11px] text-[var(--color-faint)]">
                    Attached to {detail.data.assigned_agent_ids.length} agent(s)
                  </div>
                ) : null}
              </div>
            </Section>
          ) : (
            <div className="card">
              <EmptyState title="Select a skill" hint="Pick a skill on the left to inspect its instructions." />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
