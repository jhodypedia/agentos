"use client";

import { useState } from "react";

import { EmptyState, ErrorNote, Pill, Section, Stat } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate, formatNumber, formatUsd } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { Provider } from "@/lib/types";

interface Health {
  ok: boolean;
  detail: string;
  models: string[];
  latency_ms: number | null;
}

interface Usage {
  requests: number;
  total_tokens: number;
  cost_usd: string;
}

const TYPES = [
  "openai",
  "openai_compatible",
  "codex_responses",
  "anthropic",
  "google",
  "openrouter",
  "local",
] as const;

export default function ModelsPage() {
  const providers = useApi<Provider[]>("/providers");
  const { can } = useAuth();
  const [health, setHealth] = useState<Record<string, Health>>({});
  const [usage, setUsage] = useState<Record<string, Usage>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    slug: "",
    name: "",
    provider_type: "openai_compatible",
    base_url: "",
    api_key: "",
    default_model: "",
    enabled: true,
  });

  async function checkHealth(provider: Provider) {
    setBusy(`health:${provider.id}`);
    setError(null);
    try {
      const result = await apiFetch<Health>(`/providers/${provider.id}/health`, { method: "POST" });
      setHealth((current) => ({ ...current, [provider.id]: result }));
      providers.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Health check failed");
    } finally {
      setBusy(null);
    }
  }

  async function loadUsage(provider: Provider) {
    setBusy(`usage:${provider.id}`);
    setError(null);
    try {
      const result = await apiFetch<Usage>(`/providers/${provider.id}/usage`);
      setUsage((current) => ({ ...current, [provider.id]: result }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Usage query failed");
    } finally {
      setBusy(null);
    }
  }

  async function toggle(provider: Provider) {
    setBusy(`toggle:${provider.id}`);
    setError(null);
    try {
      await apiFetch(`/providers/${provider.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !provider.enabled }),
      });
      providers.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Update failed");
    } finally {
      setBusy(null);
    }
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy("create");
    setError(null);
    try {
      await apiFetch("/providers", {
        method: "POST",
        body: JSON.stringify({
          slug: form.slug,
          name: form.name,
          provider_type: form.provider_type,
          base_url: form.base_url || null,
          api_key: form.api_key || null,
          default_model: form.default_model || null,
          enabled: form.enabled,
        }),
      });
      setForm({ slug: "", name: "", provider_type: "openai_compatible", base_url: "", api_key: "", default_model: "", enabled: true });
      setShowForm(false);
      providers.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not register the provider");
    } finally {
      setBusy(null);
    }
  }

  const enabled = (providers.data ?? []).filter((provider) => provider.enabled).length;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Model manager</h1>
          <p className="mt-0.5 text-xs text-[var(--color-faint)]">
            Provider credentials are encrypted at rest and never returned by the API.
          </p>
        </div>
        {can("admin") ? (
          <button type="button" className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
            {showForm ? "Cancel" : "Add provider"}
          </button>
        ) : null}
      </header>

      <ErrorNote message={error ?? providers.error} />

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Providers" value={providers.data?.length ?? 0} hint={`${enabled} enabled`} />
        <Stat label="Models seen" value={Object.values(health).reduce((sum, item) => sum + item.models.length, 0)} hint="From the last health check in this session" />
        <Stat
          label="Requests billed"
          value={formatNumber(Object.values(usage).reduce((sum, item) => sum + item.requests, 0))}
          hint="Loaded on demand"
        />
      </div>

      {showForm ? (
        <Section title="Register provider">
          <form onSubmit={create} className="grid gap-3 px-5 py-4 md:grid-cols-2">
            <label className="space-y-1.5">
              <span className="label">Slug</span>
              <input className="input" required pattern="[a-z0-9][a-z0-9_-]*" value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} />
            </label>
            <label className="space-y-1.5">
              <span className="label">Name</span>
              <input className="input" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </label>
            <label className="space-y-1.5">
              <span className="label">Type</span>
              <select className="input" value={form.provider_type} onChange={(e) => setForm({ ...form, provider_type: e.target.value })}>
                {TYPES.map((type) => (
                  <option key={type} value={type}>
                    {type.replaceAll("_", " ")}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1.5">
              <span className="label">Default model</span>
              <input className="input" value={form.default_model} onChange={(e) => setForm({ ...form, default_model: e.target.value })} />
            </label>
            <label className="space-y-1.5 md:col-span-2">
              <span className="label">Base URL</span>
              <input className="input" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
            </label>
            <label className="space-y-1.5 md:col-span-2">
              <span className="label">API key</span>
              <input
                className="input font-mono"
                type="password"
                autoComplete="off"
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                placeholder="Stored encrypted; only a hint is ever returned"
              />
            </label>
            <div className="md:col-span-2">
              <button type="submit" className="btn btn-primary" disabled={busy === "create"}>
                {busy === "create" ? "Saving…" : "Register"}
              </button>
            </div>
          </form>
        </Section>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-2">
        {providers.data?.map((provider) => {
          const probe = health[provider.id];
          const used = usage[provider.id];
          return (
            <div key={provider.id} className="card">
              <div className="flex items-start justify-between gap-3 border-b border-[var(--color-line)] px-5 py-3.5">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium">{provider.name}</span>
                    <Pill tone={provider.enabled ? "ok" : "neutral"}>{provider.enabled ? "enabled" : "disabled"}</Pill>
                  </div>
                  <div className="mono mt-0.5 truncate text-[10px] text-[var(--color-faint)]">
                    {provider.slug} · {provider.provider_type.replaceAll("_", " ")}
                  </div>
                </div>
                {can("admin") ? (
                  <button type="button" className="btn px-2 py-0.5 text-[11px]" disabled={busy !== null} onClick={() => toggle(provider)}>
                    {provider.enabled ? "Disable" : "Enable"}
                  </button>
                ) : null}
              </div>

              <dl className="divide-y divide-[var(--color-line)] text-xs">
                {[
                  ["Base URL", provider.base_url ?? "default"],
                  ["Default model", provider.default_model ?? "unset"],
                  ["Key", provider.api_key_hint ?? "not set"],
                  ["Last check", formatDate(provider.last_health_check_at)],
                  ["Status", provider.health_status ?? "unknown"],
                ].map(([key, value]) => (
                  <div key={key} className="flex items-center justify-between gap-3 px-5 py-1.5">
                    <dt className="text-[var(--color-faint)]">{key}</dt>
                    <dd className="truncate text-right text-[11px]">{value}</dd>
                  </div>
                ))}
              </dl>

              {probe ? (
                <div className="border-t border-[var(--color-line)] px-5 py-2.5 text-[11px]">
                  <div className="flex items-center gap-2">
                    <Pill tone={probe.ok ? "ok" : "danger"}>{probe.ok ? "reachable" : "unreachable"}</Pill>
                    {probe.latency_ms != null ? <span className="text-[var(--color-faint)]">{probe.latency_ms}ms</span> : null}
                  </div>
                  {probe.detail ? <p className="mt-1 text-[var(--color-faint)]">{probe.detail}</p> : null}
                  {probe.models.length > 0 ? (
                    <div className="mono mt-1 flex flex-wrap gap-1">
                      {probe.models.slice(0, 8).map((model) => (
                        <span key={model} className="rounded bg-[var(--color-canvas)] px-1.5 py-0.5 text-[10px]">
                          {model}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}

              {used ? (
                <div className="border-t border-[var(--color-line)] px-5 py-2.5 text-[11px]">
                  <div className="flex items-center justify-between">
                    <span className="text-[var(--color-faint)]">
                      {formatNumber(used.requests)} requests · {formatNumber(used.total_tokens)} tokens
                    </span>
                    <span className="tabular-nums">{formatUsd(used.cost_usd)}</span>
                  </div>
                </div>
              ) : null}

              {can("operator") ? (
                <div className="flex gap-2 border-t border-[var(--color-line)] px-5 py-2.5">
                  <button
                    type="button"
                    className="btn px-2 py-0.5 text-[11px]"
                    disabled={busy !== null}
                    onClick={() => checkHealth(provider)}
                  >
                    {busy === `health:${provider.id}` ? "Checking…" : "Health check"}
                  </button>
                  <button
                    type="button"
                    className="btn px-2 py-0.5 text-[11px]"
                    disabled={busy !== null}
                    onClick={() => loadUsage(provider)}
                  >
                    {busy === `usage:${provider.id}` ? "Loading…" : "Usage"}
                  </button>
                </div>
              ) : null}
            </div>
          );
        })}
      </div>

      {!providers.loading && providers.data?.length === 0 ? (
        <div className="card">
          <EmptyState
            title="No providers configured"
            hint="Register a provider with its base URL and API key before agents can call a model."
          />
        </div>
      ) : null}
    </div>
  );
}
