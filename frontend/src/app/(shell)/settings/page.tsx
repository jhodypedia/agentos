"use client";

import { useState } from "react";

import { EmptyState, ErrorNote, Pill, Section } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { Setting, SettingList } from "@/lib/types";

export default function SettingsPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin" || user?.role === "owner";
  const settings = useApi<SettingList>(isAdmin ? "/settings" : null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  function valueText(setting: Setting): string {
    const raw = setting.value["value"] ?? setting.value;
    return JSON.stringify(raw);
  }

  async function save(setting: Setting) {
    const draft = drafts[setting.key];
    if (draft === undefined) return;
    setBusy(setting.key);
    setError(null);
    setSaved(null);
    try {
      let parsed: unknown;
      try {
        parsed = JSON.parse(draft);
      } catch {
        parsed = draft;
      }
      await apiFetch(`/settings/${setting.key}`, {
        method: "PATCH",
        body: JSON.stringify({ value: { value: parsed } }),
      });
      setSaved(setting.key);
      settings.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Save failed");
    } finally {
      setBusy(null);
    }
  }

  if (!isAdmin) {
    return (
      <div className="space-y-5">
        <header>
          <h1 className="text-lg font-semibold tracking-tight">Settings</h1>
        </header>
        <div className="card">
          <EmptyState
            title="Administrator access required"
            hint="Platform settings are visible to administrators only."
          />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Settings</h1>
        <p className="mt-0.5 text-xs text-[var(--color-faint)]">
          Stored overrides. Secrets live in the environment and are reported as configured or not.
        </p>
      </header>

      <ErrorNote message={error ?? settings.error} />
      {saved ? (
        <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-ok)_40%,var(--color-line))] bg-[color-mix(in_srgb,var(--color-ok)_8%,transparent)] px-3 py-2 text-xs text-[var(--color-ok)]">
          Saved {saved}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <Section title="Editable settings" description="Allow-listed keys only">
            {settings.data && settings.data.items.length > 0 ? (
              <ul className="divide-y divide-[var(--color-line)]">
                {settings.data.items.map((setting) => (
                  <li key={setting.key} className="px-5 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="mono text-xs">{setting.key}</span>
                      {setting.is_secret ? <Pill tone="warn">secret</Pill> : null}
                      {setting.editable ? <Pill tone="ok">editable</Pill> : <Pill>read-only</Pill>}
                    </div>
                    <p className="mt-1 text-[11px] text-[var(--color-faint)]">{setting.description}</p>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <input
                        className="input mono max-w-md flex-1"
                        disabled={!setting.editable}
                        value={drafts[setting.key] ?? valueText(setting)}
                        onChange={(event) => setDrafts({ ...drafts, [setting.key]: event.target.value })}
                      />
                      {setting.editable ? (
                        <button
                          type="button"
                          className="btn px-2.5 py-1 text-[11px]"
                          disabled={busy !== null || drafts[setting.key] === undefined}
                          onClick={() => save(setting)}
                        >
                          {busy === setting.key ? "Saving…" : "Save"}
                        </button>
                      ) : null}
                      <span className="text-[10px] text-[var(--color-faint)]">
                        updated {formatDate(setting.updated_at)}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState
                title={settings.loading ? "Loading settings…" : "No settings rows"}
                hint="Run the seed command to populate the default settings."
              />
            )}
          </Section>
        </div>

        <div className="space-y-4">
          <Section title="Environment" description="Read-only, from process configuration">
            {settings.data ? (
              <dl className="divide-y divide-[var(--color-line)] text-xs">
                {Object.entries(settings.data.environment).map(([key, value]) => (
                  <div key={key} className="flex items-center justify-between gap-3 px-5 py-1.5">
                    <dt className="truncate text-[var(--color-faint)]">{key.replaceAll("_", " ")}</dt>
                    <dd className="mono truncate text-right text-[11px]">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="px-5 py-6 text-xs text-[var(--color-faint)]">Unavailable</p>
            )}
          </Section>
        </div>
      </div>
    </div>
  );
}
