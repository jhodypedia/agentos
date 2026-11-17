"use client";

import { useState } from "react";

import { EmptyState, ErrorNote, Pill, Section, Stat, StatusPill } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cx, formatDate, severityTone, truncate } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { Finding, Project, Scan, ScannerInfo, Scope } from "@/lib/types";

const SEVERITIES = ["critical", "high", "medium", "low", "info"] as const;

export default function ResearchPage() {
  const findings = useApi<Finding[]>("/research/findings?limit=200");
  const scanners = useApi<ScannerInfo[]>("/research/scanners");
  const scopes = useApi<Scope[]>("/research/scopes?limit=100");
  const scans = useApi<Scan[]>("/research/scans?limit=50");
  const projects = useApi<Project[]>("/projects");
  const { can } = useAuth();
  const [severity, setSeverity] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [scanForm, setScanForm] = useState({ scope: "", tool: "semgrep", target: "." });

  async function startScan(event: React.FormEvent) {
    event.preventDefault();
    if (!scanForm.scope) {
      setError("Create and verify a scope before scanning");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await apiFetch<Scan>("/research/scans", {
        method: "POST",
        body: JSON.stringify({
          scope_id: scanForm.scope,
          tool: scanForm.tool,
          target_path: scanForm.target,
        }),
      });
      scans.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Scan failed");
    } finally {
      setBusy(false);
    }
  }

  async function scopeAction(scopeId: string, action: "verify" | "revoke") {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/research/scopes/${scopeId}/${action}`, { method: "POST" });
      scopes.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : `${action} failed`);
    } finally {
      setBusy(false);
    }
  }

  const filtered = (findings.data ?? []).filter((item) => !severity || item.severity === severity);
  const counts = Object.fromEntries(
    SEVERITIES.map((name) => [
      name,
      (findings.data ?? []).filter((item) => item.severity === name).length,
    ]),
  ) as Record<string, number>;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Security research</h1>
        <p className="mt-0.5 text-xs text-[var(--color-faint)]">
          Authorized scopes, sandboxed scanners and the findings under review.
        </p>
      </header>

      <ErrorNote message={error ?? findings.error ?? scopes.error} />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Critical" value={counts.critical ?? 0} tone={counts.critical ? "danger" : undefined} />
        <Stat label="High" value={counts.high ?? 0} tone={counts.high ? "danger" : undefined} />
        <Stat label="Verified scopes" value={(scopes.data ?? []).filter((scope) => scope.verified_at).length} hint={`${scopes.data?.length ?? 0} registered`} />
        <Stat label="Scans run" value={scans.data?.length ?? 0} hint="Most recent 50" />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <Section title="Findings" description="Ordered by severity then confidence">
            <div className="flex flex-wrap gap-1.5 border-b border-[var(--color-line)] px-5 py-2.5">
              <button
                type="button"
                className={cx("btn px-2 py-0.5 text-[11px]", severity === "" ? "border-[var(--color-accent)] text-[var(--color-accent)]" : "")}
                onClick={() => setSeverity("")}
              >
                All ({(findings.data ?? []).length})
              </button>
              {SEVERITIES.map((name) => (
                <button
                  key={name}
                  type="button"
                  className={cx("btn px-2 py-0.5 text-[11px] capitalize", severity === name ? "border-[var(--color-accent)] text-[var(--color-accent)]" : "")}
                  onClick={() => setSeverity(name)}
                >
                  {name} ({counts[name] ?? 0})
                </button>
              ))}
            </div>
            {filtered.length === 0 ? (
              <EmptyState
                title={findings.loading ? "Loading findings…" : "No findings"}
                hint="Findings appear when an operator records one or imports a scanner result."
              />
            ) : (
              <ul className="divide-y divide-[var(--color-line)]">
                {filtered.map((finding) => (
                  <li key={finding.id} className="px-5 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Pill tone={severityTone(finding.severity)}>{finding.severity}</Pill>
                      <span className="text-xs font-medium">{truncate(finding.title, 70)}</span>
                      <StatusPill status={finding.status} />
                      <span className="ml-auto text-[10px] text-[var(--color-faint)]">
                        confidence {(finding.confidence * 100).toFixed(0)}%
                        {finding.cvss_score ? ` · CVSS ${finding.cvss_score}` : ""}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-[var(--color-muted)]">
                      {finding.description || finding.hypothesis}
                    </p>
                    <div className="mt-1 flex flex-wrap gap-3 text-[10px] text-[var(--color-faint)]">
                      <span className="capitalize">{finding.category.replaceAll("_", " ")}</span>
                      {finding.cwe_id ? <span className="mono">{finding.cwe_id}</span> : null}
                      <span>{formatDate(finding.created_at)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Scopes" description="Written authorization gates every scan">
            {scopes.data && scopes.data.length > 0 ? (
              <ul className="divide-y divide-[var(--color-line)]">
                {scopes.data.map((scope) => {
                  const state = scope.revoked_at ? "revoked" : scope.verified_at ? "verified" : "unverified";
                  return (
                    <li key={scope.id} className="px-5 py-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs font-medium">{scope.name}</span>
                        <Pill tone={state === "verified" ? "ok" : state === "revoked" ? "danger" : "warn"}>
                          {state}
                        </Pill>
                        <span className="mono text-[10px] text-[var(--color-faint)]">{scope.local_path}</span>
                        {can("admin") ? (
                          <div className="ml-auto flex gap-1.5">
                            {!scope.verified_at && !scope.revoked_at ? (
                              <button
                                type="button"
                                className="btn px-2 py-0.5 text-[11px]"
                                disabled={busy}
                                onClick={() => scopeAction(scope.id, "verify")}
                              >
                                Verify
                              </button>
                            ) : null}
                            {!scope.revoked_at ? (
                              <button
                                type="button"
                                className="btn btn-danger px-2 py-0.5 text-[11px]"
                                disabled={busy}
                                onClick={() => scopeAction(scope.id, "revoke")}
                              >
                                Revoke
                              </button>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                      <p className="mt-1 line-clamp-1 text-[11px] text-[var(--color-faint)]">
                        {truncate(scope.authorization, 160)}
                      </p>
                      <div className="mt-1 text-[10px] text-[var(--color-faint)]">
                        Tools: {scope.allowed_tools.length ? scope.allowed_tools.join(", ") : "none"} ·{" "}
                        {scope.expires_at ? `expires ${formatDate(scope.expires_at)}` : "no expiry"}
                      </div>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <EmptyState
                title="No scopes registered"
                hint="A scope records the authorization text that permits scanning a target."
              />
            )}
          </Section>
        </div>

        <div className="space-y-4">
          <Section title="Scanners" description="Probed inside the sandbox image">
            <ul className="divide-y divide-[var(--color-line)]">
              {(scanners.data ?? []).map((scanner) => (
                <li key={scanner.name} className="px-5 py-2">
                  <div className="flex items-center justify-between">
                    <span className="mono text-xs">{scanner.name}</span>
                    <Pill tone={scanner.available ? "ok" : "neutral"}>
                      {scanner.available ? "available" : "unavailable"}
                    </Pill>
                  </div>
                  {scanner.reason ? (
                    <p className="mt-0.5 text-[10px] text-[var(--color-faint)]">{scanner.reason}</p>
                  ) : null}
                </li>
              ))}
              {scanners.data?.length === 0 ? (
                <li className="px-5 py-5 text-xs text-[var(--color-faint)]">No scanners reported</li>
              ) : null}
            </ul>
          </Section>

          {can("admin") ? (
            <Section title="Run a scan" description="Admin only, offline sandbox">
              <form onSubmit={startScan} className="space-y-2.5 px-5 py-4">
                <label className="block space-y-1.5">
                  <span className="label">Scope</span>
                  <select
                    className="input"
                    value={scanForm.scope}
                    onChange={(event) => setScanForm({ ...scanForm, scope: event.target.value })}
                  >
                    <option value="">Select a verified scope</option>
                    {(scopes.data ?? [])
                      .filter((scope) => scope.verified_at && !scope.revoked_at)
                      .map((scope) => (
                        <option key={scope.id} value={scope.id}>
                          {scope.name}
                        </option>
                      ))}
                  </select>
                </label>
                <label className="block space-y-1.5">
                  <span className="label">Scanner</span>
                  <select
                    className="input"
                    value={scanForm.tool}
                    onChange={(event) => setScanForm({ ...scanForm, tool: event.target.value })}
                  >
                    {(scanners.data ?? [])
                      .filter((scanner) => scanner.available)
                      .map((scanner) => (
                        <option key={scanner.name} value={scanner.name}>
                          {scanner.name}
                        </option>
                      ))}
                  </select>
                </label>
                <label className="block space-y-1.5">
                  <span className="label">Target path</span>
                  <input
                    className="input"
                    value={scanForm.target}
                    onChange={(event) => setScanForm({ ...scanForm, target: event.target.value })}
                  />
                </label>
                <button type="submit" className="btn btn-primary w-full" disabled={busy}>
                  {busy ? "Scanning…" : "Start scan"}
                </button>
                <p className="text-[10px] leading-4 text-[var(--color-faint)]">
                  Scanning runs in a network-isolated container. Findings are not created
                  automatically; import a scan to turn results into hypotheses.
                </p>
              </form>
            </Section>
          ) : null}

          <Section title="Recent scans">
            {scans.data && scans.data.length > 0 ? (
              <ul className="divide-y divide-[var(--color-line)]">
                {scans.data.slice(0, 8).map((scan) => (
                  <li key={scan.id} className="px-5 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="mono text-[11px]">{scan.tool}</span>
                      <Pill tone={scan.status === "completed" ? "ok" : scan.status === "failed" ? "danger" : "accent"}>
                        {scan.status}
                      </Pill>
                    </div>
                    <div className="mt-0.5 truncate text-[10px] text-[var(--color-faint)]">
                      {scan.target_path} · {formatDate(scan.created_at)}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="px-5 py-5 text-xs text-[var(--color-faint)]">No scans yet</p>
            )}
          </Section>

          <Section title="Projects">
            <ul className="divide-y divide-[var(--color-line)]">
              {(projects.data ?? []).slice(0, 6).map((project) => (
                <li key={project.id} className="flex items-center justify-between px-5 py-2 text-xs">
                  <span className="truncate">{project.name}</span>
                  <span className="mono text-[10px] text-[var(--color-faint)]">{project.slug}</span>
                </li>
              ))}
              {projects.data?.length === 0 ? (
                <li className="px-5 py-5 text-xs text-[var(--color-faint)]">No projects</li>
              ) : null}
            </ul>
          </Section>
        </div>
      </div>
    </div>
  );
}
