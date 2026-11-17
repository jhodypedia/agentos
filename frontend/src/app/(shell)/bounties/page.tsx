"use client";

import { useState } from "react";

import { EmptyState, ErrorNote, Pill, Stat } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cx, formatDate, formatUsd, truncate } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { Competition, CompetitionEntry, CompetitionSyncResult } from "@/lib/types";

const TABS = ["board", "entries"] as const;
const STATUSES = ["discovered", "evaluating", "in_progress", "submitted", "graded", "closed", "archived"] as const;

export default function BountiesPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]>("board");
  const [status, setStatus] = useState("");
  const competitions = useApi<Competition[]>(
    `/bounties/competitions?limit=200${status ? `&status=${status}` : ""}`,
    [status],
  );
  const entries = useApi<CompetitionEntry[]>("/bounties/entries?limit=100");
  const { can } = useAuth();
  const [sync, setSync] = useState<CompetitionSyncResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function syncBoard() {
    setBusy(true);
    setError(null);
    setSync(null);
    try {
      const result = await apiFetch<CompetitionSyncResult>("/bounties/competitions/sync", {
        method: "POST",
      });
      setSync(result);
      competitions.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sync failed");
    } finally {
      setBusy(false);
    }
  }

  const totalReward = (competitions.data ?? []).reduce(
    (sum, item) => sum + Number(item.reward_amount ?? 0),
    0,
  );
  const open = (competitions.data ?? []).filter(
    (item) => !["closed", "archived"].includes(item.status),
  ).length;
  const awarded = (entries.data ?? []).reduce((sum, item) => sum + Number(item.prize_usd ?? 0), 0);

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Bounty explorer</h1>
          <p className="mt-0.5 text-xs text-[var(--color-faint)]">
            Competitions synced from make.fun, and the entries this platform has staged.
          </p>
        </div>
        {can("operator") ? (
          <button type="button" className="btn" onClick={syncBoard} disabled={busy}>
            {busy ? "Syncing…" : "Sync board"}
          </button>
        ) : null}
      </header>

      <ErrorNote message={error} />

      {sync ? (
        <div className="card card-pad text-xs text-[var(--color-muted)]">
          Synced {sync.total} competitions · {sync.created.length} new · {sync.updated.length} updated
        </div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Competitions" value={competitions.data?.length ?? 0} hint={`${open} open`} />
        <Stat label="Listed rewards" value={formatUsd(totalReward)} hint="Sum of published amounts" />
        <Stat label="Prizes won" value={formatUsd(awarded)} hint={`${entries.data?.length ?? 0} entries`} />
      </div>

      <div className="flex gap-1.5">
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            onClick={() => setTab(name)}
            className={cx(
              "btn px-3 py-1 text-[11px] capitalize",
              tab === name ? "border-[var(--color-accent)] text-[var(--color-accent)]" : "",
            )}
          >
            {name}
          </button>
        ))}
      </div>

      {tab === "board" ? (
        <>
          <div className="flex flex-wrap gap-1.5">
            <button
              type="button"
              className={cx("btn px-2.5 py-1 text-[11px]", status === "" ? "border-[var(--color-accent)] text-[var(--color-accent)]" : "")}
              onClick={() => setStatus("")}
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
                onClick={() => setStatus(name)}
              >
                {name.replaceAll("_", " ")}
              </button>
            ))}
          </div>

          <div className="card overflow-x-auto">
            {competitions.data && competitions.data.length > 0 ? (
              <table className="data">
                <thead>
                  <tr>
                    <th>Competition</th>
                    <th>Status</th>
                    <th>Reward</th>
                    <th>Deadline</th>
                    <th>Synced</th>
                  </tr>
                </thead>
                <tbody>
                  {competitions.data.map((item) => (
                    <tr key={item.id}>
                      <td className="max-w-lg">
                        <div className="truncate text-xs">{item.title}</div>
                        <a
                          href={item.url}
                          target="_blank"
                          rel="noreferrer noopener"
                          className="mono text-[10px] text-[var(--color-faint)] hover:text-[var(--color-accent)]"
                        >
                          {item.external_id}
                        </a>
                      </td>
                      <td>
                        <Pill tone={["closed", "archived"].includes(item.status) ? "neutral" : "accent"}>
                          {item.status.replaceAll("_", " ")}
                        </Pill>
                      </td>
                      <td className="tabular-nums">
                        {item.reward_amount ? `${item.reward_amount} ${item.reward_currency}` : "—"}
                      </td>
                      <td className="whitespace-nowrap text-[var(--color-faint)]">
                        {formatDate(item.deadline_at)}
                      </td>
                      <td className="whitespace-nowrap text-[var(--color-faint)]">{formatDate(item.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState
                title={competitions.loading ? "Loading competitions…" : "No competitions synced"}
                hint={
                  can("operator")
                    ? "Run a sync to pull the current board from make.fun. It requires a configured API key."
                    : "An operator must sync the board first."
                }
              />
            )}
          </div>
        </>
      ) : (
        <div className="card overflow-x-auto">
          {entries.data && entries.data.length > 0 ? (
            <table className="data">
              <thead>
                <tr>
                  <th>Entry</th>
                  <th>Status</th>
                  <th>Score</th>
                  <th>Rank</th>
                  <th>Prize</th>
                  <th>Submitted</th>
                </tr>
              </thead>
              <tbody>
                {entries.data.map((entry) => (
                  <tr key={entry.id}>
                    <td className="max-w-md">
                      <div className="truncate text-xs">{entry.title || truncate(entry.repo_url, 60)}</div>
                      <div className="mono truncate text-[10px] text-[var(--color-faint)]">
                        {entry.commit_sha.slice(0, 12)}
                      </div>
                      {entry.error ? (
                        <div className="truncate text-[10px] text-[var(--color-danger)]">{entry.error}</div>
                      ) : null}
                    </td>
                    <td>
                      <Pill
                        tone={
                          entry.status === "graded" || entry.status === "accepted"
                            ? "ok"
                            : entry.status.includes("awaiting")
                              ? "warn"
                              : "neutral"
                        }
                      >
                        {entry.status.replaceAll("_", " ")}
                      </Pill>
                    </td>
                    <td className="tabular-nums">{entry.score ?? "—"}</td>
                    <td className="tabular-nums">{entry.rank ?? "—"}</td>
                    <td className="tabular-nums">{entry.prize_usd ? formatUsd(entry.prize_usd) : "—"}</td>
                    <td className="whitespace-nowrap text-[var(--color-faint)]">
                      {formatDate(entry.submitted_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState
              title={entries.loading ? "Loading entries…" : "No entries staged"}
              hint="Prepare an entry from a competition to request human approval before submission."
            />
          )}
        </div>
      )}
    </div>
  );
}
