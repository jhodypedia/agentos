"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { ErrorNote } from "@/components/ui";
import { login } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function LoginPage() {
  const { signIn } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await login(email, password);
      await signIn(result.access_token);
      router.replace("/");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2.5">
          <div className="grid h-8 w-8 place-items-center rounded-md bg-[var(--color-accent)] text-sm font-bold text-[#08121f]">
            A
          </div>
          <div>
            <div className="text-sm font-semibold tracking-tight">AgentOS</div>
            <div className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-faint)]">
              Control plane
            </div>
          </div>
        </div>

        <form onSubmit={submit} className="card card-pad space-y-3">
          <div className="space-y-1.5">
            <label className="label" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              className="input"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="admin@example.com"
            />
          </div>
          <div className="space-y-1.5">
            <label className="label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              className="input"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
          <ErrorNote message={error} />
          <button type="submit" className="btn btn-primary w-full" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
          <p className="text-[11px] leading-relaxed text-[var(--color-faint)]">
            The token is kept in this browser only and is sent to the API with each request.
          </p>
        </form>
      </div>
    </div>
  );
}
