"use client";

import { useEffect, useRef, useState } from "react";

import { EmptyState, ErrorNote, Pill, Section } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { CodingSession, CommandResult, Project, WorkspaceListing } from "@/lib/types";

interface Entry {
  id: number;
  command: string;
  result: CommandResult | null;
  error: string | null;
}

export default function TerminalPage() {
  const projects = useApi<Project[]>("/projects");
  const { can } = useAuth();
  const [projectId, setProjectId] = useState<string>("");
  const workspace = useApi<WorkspaceListing>(projectId ? `/projects/${projectId}/workspace` : null, [projectId]);
  const sessions = useApi<CodingSession[]>(
    projectId ? `/projects/${projectId}/coding/sessions?limit=20` : null,
    [projectId],
  );
  const [command, setCommand] = useState("");
  const [history, setHistory] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [instructions, setInstructions] = useState("");
  const [running, setRunning] = useState(false);
  const [codingError, setCodingError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId && projects.data && projects.data.length > 0) {
      setProjectId(projects.data[0]?.id ?? "");
    }
  }, [projectId, projects.data]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [history]);

  async function run(event: React.FormEvent) {
    event.preventDefault();
    if (!projectId || !command.trim()) return;
    setBusy(true);
    setError(null);
    const entry: Entry = { id: Date.now(), command: command.trim(), result: null, error: null };
    setHistory((current) => [...current, entry]);
    setCommand("");
    try {
      const result = await apiFetch<CommandResult>(`/projects/${projectId}/commands`, {
        method: "POST",
        body: JSON.stringify({ command: entry.command, timeout_seconds: 60 }),
      });
      setHistory((current) => current.map((item) => (item.id === entry.id ? { ...item, result } : item)));
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "Command failed";
      setHistory((current) => current.map((item) => (item.id === entry.id ? { ...item, error: message } : item)));
    } finally {
      setBusy(false);
    }
  }

  async function runCoding(event: React.FormEvent) {
    event.preventDefault();
    if (!projectId || !instructions.trim()) return;
    setRunning(true);
    setCodingError(null);
    try {
      await apiFetch(`/projects/${projectId}/coding/run`, {
        method: "POST",
        body: JSON.stringify({ instruction: instructions, timeout_seconds: 300 }),
      });
      setInstructions("");
      sessions.reload();
    } catch (cause) {
      setCodingError(cause instanceof Error ? cause.message : "Coding engine failed");
    } finally {
      setRunning(false);
    }
  }

  const selected = (projects.data ?? []).find((project) => project.id === projectId);

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Terminal</h1>
          <p className="mt-0.5 text-xs text-[var(--color-faint)]">
            Run commands inside a project workspace and review coding-engine sessions.
          </p>
        </div>
        <select className="input max-w-xs" value={projectId} onChange={(event) => setProjectId(event.target.value)}>
          <option value="">Select a project</option>
          {(projects.data ?? []).map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      </header>

      <ErrorNote message={error ?? projects.error} />

      {projects.data && projects.data.length === 0 ? (
        <div className="card">
          <EmptyState
            title="No projects yet"
            hint="Create a project before opening a workspace terminal. Projects are created through the API or by syncing bounties."
          />
        </div>
      ) : null}

      {projectId && selected ? (
        <div className="grid gap-4 xl:grid-cols-3">
          <div className="space-y-4 xl:col-span-2">
            <Section
              title="Command runner"
              description={selected.workspace_path}
              action={
                workspace.data ? (
                  <span className="text-[11px] text-[var(--color-faint)]">
                    {workspace.data.file_count} files · {(workspace.data.total_bytes / 1024).toFixed(0)} KB ·{" "}
                    {workspace.data.repositories} repo(s)
                  </span>
                ) : null
              }
            >
              {!can("admin") ? (
                <p className="px-5 py-6 text-xs text-[var(--color-faint)]">
                  The command runner requires the admin role.
                </p>
              ) : (
                <>
                  <div
                    ref={scrollRef}
                    className="mono h-72 overflow-y-auto whitespace-pre-wrap bg-[var(--color-canvas)] px-4 py-3 text-[12px] leading-5"
                  >
                    {history.length === 0 ? (
                      <span className="text-[var(--color-faint)]">
                        Commands run in the project workspace ({workspace.data?.exists ? "present" : "not initialized"}).
                      </span>
                    ) : (
                      history.map((entry) => (
                        <div key={entry.id} className="mb-3">
                          <div className="text-[var(--color-accent)]">$ {entry.command}</div>
                          {entry.error ? (
                            <div className="text-[var(--color-danger)]">{entry.error}</div>
                          ) : entry.result ? (
                            <>
                              {entry.result.stdout ? <div>{entry.result.stdout}</div> : null}
                              {entry.result.stderr ? (
                                <div className="text-[var(--color-warn)]">{entry.result.stderr}</div>
                              ) : null}
                              <div className="text-[var(--color-faint)]">
                                exit {entry.result.exit_code} · {entry.result.duration_ms}ms ·{" "}
                                {entry.result.sandbox}
                                {entry.result.timed_out ? " · timed out" : ""}
                                {entry.result.truncated ? " · truncated" : ""}
                              </div>
                            </>
                          ) : (
                            <div className="text-[var(--color-faint)]">running…</div>
                          )}
                        </div>
                      ))
                    )}
                  </div>
                  <form onSubmit={run} className="flex gap-2 border-t border-[var(--color-line)] px-4 py-3">
                    <span className="mono grid place-items-center text-[var(--color-faint)]">$</span>
                    <input
                      className="mono flex-1 bg-transparent text-[12px] outline-none"
                      value={command}
                      onChange={(event) => setCommand(event.target.value)}
                      placeholder="ls -la"
                      spellCheck={false}
                      autoComplete="off"
                    />
                    <button type="submit" className="btn px-2.5 py-1 text-[11px]" disabled={busy || !command.trim()}>
                      {busy ? "Running…" : "Run"}
                    </button>
                  </form>
                </>
              )}
            </Section>

            {can("operator") ? (
              <Section title="Coding engine" description="Delegate an instruction to the configured coding engine">
                <form onSubmit={runCoding} className="space-y-2.5 px-5 py-4">
                  <textarea
                    className="input min-h-20"
                    value={instructions}
                    onChange={(event) => setInstructions(event.target.value)}
                    placeholder="Add a /health endpoint returning build metadata"
                  />
                  <ErrorNote message={codingError} />
                  <button type="submit" className="btn btn-primary" disabled={running || !instructions.trim()}>
                    {running ? "Running…" : "Start session"}
                  </button>
                </form>
              </Section>
            ) : null}
          </div>

          <div className="space-y-4">
            <Section title="Workspace">
              <dl className="divide-y divide-[var(--color-line)] text-xs">
                {[
                  ["Path", workspace.data?.path ?? "—"],
                  ["Initialized", workspace.data?.exists ? "yes" : "no"],
                  ["Files", String(workspace.data?.file_count ?? 0)],
                  ["Repositories", String(workspace.data?.repositories ?? 0)],
                ].map(([key, value]) => (
                  <div key={key} className="flex items-center justify-between gap-3 px-5 py-2">
                    <dt className="text-[var(--color-faint)]">{key}</dt>
                    <dd className="mono truncate text-[11px]">{value}</dd>
                  </div>
                ))}
              </dl>
            </Section>

            <Section title="Coding sessions">
              {sessions.data && sessions.data.length > 0 ? (
                <ul className="divide-y divide-[var(--color-line)]">
                  {sessions.data.map((session) => (
                    <li key={session.id} className="px-5 py-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <span className="mono text-[11px]">{session.engine}</span>
                        <Pill tone={session.exit_code === 0 ? "ok" : "danger"}>exit {session.exit_code}</Pill>
                      </div>
                      <div className="mt-0.5 truncate text-[11px] text-[var(--color-muted)]">
                        {session.instruction}
                      </div>
                      <div className="text-[10px] text-[var(--color-faint)]">
                        {formatDate(session.created_at)} · {session.duration_ms}ms · {session.changes.length} change(s)
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="px-5 py-5 text-xs text-[var(--color-faint)]">No sessions yet</p>
              )}
            </Section>
          </div>
        </div>
      ) : null}
    </div>
  );
}
