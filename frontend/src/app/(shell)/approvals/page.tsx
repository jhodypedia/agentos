"use client";

import Link from "next/link";
import { useState } from "react";

import { EmptyState, ErrorNote, Section, StatusPill } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { cx, formatDate, relativeTime, truncate } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { Approval, ApprovalPage } from "@/lib/types";

const FILTERS = ["pending", "approved", "rejected", "expired", "cancelled"] as const;

export default function ApprovalsPage() {
  const [filter, setFilter] = useState<string>("pending");
  const approvals = useApi<ApprovalPage>(
    `/approvals?limit=100${filter ? `&status=${filter}` : ""}`,
    [filter],
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});

  async function decide(approval: Approval, approved: boolean) {
    setBusy(approval.id);
    setError(null);
    try {
      await apiFetch(`/approvals/${approval.id}/decide`, {
        method: "POST",
        body: JSON.stringify({ approved, note: notes[approval.id] ?? "" }),
      });
      approvals.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Decision failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Approvals</h1>
        <p className="mt-0.5 text-xs text-[var(--color-faint)]">
          Sensitive actions wait here until a human signs off. Nothing executes before that.
        </p>
      </header>

      <ErrorNote message={error ?? approvals.error} />

      <div className="flex flex-wrap gap-1.5">
        {FILTERS.map((name) => (
          <button
            key={name}
            type="button"
            className={cx(
              "btn px-2.5 py-1 text-[11px] capitalize",
              filter === name ? "border-[var(--color-accent)] text-[var(--color-accent)]" : "",
            )}
            onClick={() => setFilter(name)}
          >
            {name}
          </button>
        ))}
      </div>

      {approvals.data && approvals.data.items.length > 0 ? (
        <div className="space-y-3">
          {approvals.data.items.map((approval) => (
            <Section
              key={approval.id}
              title={approval.title}
              description={`${approval.action_type.replaceAll("_", " ")} · requested ${relativeTime(approval.created_at)}`}
              action={<StatusPill status={approval.status} />}
            >
              <div className="space-y-3 px-5 py-4">
                <p className="whitespace-pre-wrap text-xs leading-5 text-[var(--color-muted)]">
                  {approval.description || "No description provided"}
                </p>
                {Object.keys(approval.payload).length > 0 ? (
                  <pre className="mono max-h-48 overflow-auto rounded-lg bg-[var(--color-canvas)] p-3 text-[11px]">
                    {JSON.stringify(approval.payload, null, 2)}
                  </pre>
                ) : null}
                <div className="flex flex-wrap gap-3 text-[11px] text-[var(--color-faint)]">
                  {approval.task_id ? (
                    <Link href={`/tasks/${approval.task_id}`} className="hover:text-[var(--color-accent)]">
                      Open task
                    </Link>
                  ) : null}
                  {approval.expires_at ? <span>Expires {formatDate(approval.expires_at)}</span> : null}
                  {approval.decided_at ? <span>Decided {formatDate(approval.decided_at)}</span> : null}
                  {approval.decision_note ? <span>Note: {truncate(approval.decision_note, 120)}</span> : null}
                </div>

                {approval.status === "pending" ? (
                  <div className="space-y-2 border-t border-[var(--color-line)] pt-3">
                    <input
                      className="input"
                      placeholder="Optional decision note (recorded in the audit log)"
                      value={notes[approval.id] ?? ""}
                      onChange={(event) => setNotes({ ...notes, [approval.id]: event.target.value })}
                    />
                    <div className="flex gap-2">
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={busy !== null}
                        onClick={() => decide(approval, true)}
                      >
                        {busy === approval.id ? "Saving…" : "Approve"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger"
                        disabled={busy !== null}
                        onClick={() => decide(approval, false)}
                      >
                        Reject
                      </button>
                    </div>
                  </div>
                ) : null}
              </div>
            </Section>
          ))}
        </div>
      ) : (
        <div className="card">
          <EmptyState
            title={approvals.loading ? "Loading approvals…" : `No ${filter} approvals`}
            hint="Agents create approval requests when a tool needs a human decision."
          />
        </div>
      )}
    </div>
  );
}
