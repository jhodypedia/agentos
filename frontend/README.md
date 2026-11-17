# AgentOS dashboard

Next.js (App Router) + React 19 + TypeScript + Tailwind CSS v4 control plane
for the AgentOS API. Everything on screen comes from a real API call; there is
no mock data and no placeholder metric.

## Pages

| Route | Contents | Minimum role |
| --- | --- | --- |
| `/login` | Email/password login, stores the JWT | anonymous |
| `/` | Service health, token/cost usage, task mix, findings, audit feed, live events | viewer |
| `/agents` | Agent list, create, enable/disable | operator |
| `/agents/[id]` | Definition, tools, limits, skills, tool assignments | operator |
| `/tasks` | Task list with status/cost filters, create, cancel | operator |
| `/tasks/[id]` | Objective, plan steps, event log, cancel | operator |
| `/bounties` | Synced make.fun competitions, entry preparation | operator |
| `/research` | Scopes, hypotheses, findings, scans, reports | operator |
| `/skills` | Installed skill packages, rescan, import, delete | operator (mutations: admin) |
| `/models` | Model providers, health, usage, add/test/disable | operator |
| `/terminal` | Command runner and coding-engine sessions per project | operator (commands: admin) |
| `/approvals` | Pending/decided approval requests | operator |
| `/settings` | Editable platform settings | admin |

The nav filters itself by role (viewer < operator < admin < owner), but that is
cosmetic: every route is enforced again by the API. A viewer who types
`/settings` gets the API's `403`, not a rendered page.

## How it talks to the API

- **Base URL:** `NEXT_PUBLIC_API_URL` (build-time, inlined into the bundle).
  Empty means "same origin". Defaults to `http://localhost:8000` in the
  Docker build.
- **Route prefix:** `NEXT_PUBLIC_API_PREFIX`, default `/api/v1`.
- **Auth:** the JWT lives in `localStorage` under `agentos.access_token` and is
  sent as `Authorization: Bearer`. A `401` clears it and redirects to `/login`.
- **Live events:** `GET /api/v1/dashboard/stream` as SSE, via `EventSource`.
  `EventSource` cannot set headers, so the token is passed as `?token=`
  (verified exactly like a bearer token) and the stream is dropped when the
  user logs out. The server sends a `keepalive` comment every 20 seconds and
  the browser retries after 3 seconds.
- **CORS:** the API must list this origin in `CORS_ORIGINS`. It allows
  `http://localhost:3000` by default.

### Token storage tradeoff

The token is kept in `localStorage` rather than an HttpOnly cookie. That keeps
the dashboard a plain static SPA with no server-side session and no proxy
buffering the SSE stream, and it lets the same build be served from any host.
The cost is that a successful XSS on this origin can read the token. Mitigations
in place: no `dangerouslySetInnerHTML` anywhere, all rendered text goes through
React escaping, Markdown output from the API is shown as preformatted text
rather than injected HTML, a strict `Content-Security-Policy`-friendly build
(no inline scripts beyond Next's own), and short session TTLs. If the dashboard
is ever exposed to an untrusted network, move to a cookie-based BFF.

## Development

```bash
npm ci
npm run dev          # http://localhost:3000, expects the API on :8000
npm run typecheck    # tsc --noEmit
npm run lint         # eslint (flat config)
npm run build        # production build (standalone output)
```

Pointing at a different API:

```bash
NEXT_PUBLIC_API_URL=https://agentos.example.com npm run dev
```

## Deployment

`docker compose up -d --build` builds `web` from this directory and serves it on
`${WEB_PORT:-3000}`. `output: "standalone"` keeps the runtime image small; the
container runs as the non-root `nextjs` user with `no-new-privileges`.

`NEXT_PUBLIC_API_URL` is a **build arg**, not a runtime variable — it is baked
into the JavaScript bundle. Set it to the origin the operator's browser will
reach (e.g. `https://api.agentos.example.com`), then rebuild:

```bash
NEXT_PUBLIC_API_URL=https://api.agentos.example.com docker compose build web
```

## Deviations from the original plan

- **No shadcn/ui.** The UI is hand-written Tailwind v4 on a small `@theme`
  token set (`src/app/globals.css`) with a handful of shared primitives in
  `src/components/ui.tsx`. Reason: the component set needed here is a dozen
  primitives (pill, stat, section, empty state, error note, skeleton), and
  pulling in Radix + the shadcn CLI would add a generator and a dependency tree
  for no behaviour we do not already have.
- **No charting library.** `src/components/charts.tsx` renders the usage area
  chart and the breakdown bars as inline SVG. The data is a short daily series;
  a charting dependency would be larger than the code it replaces.
- **No React Query / SWR.** `src/lib/use-api.ts` is a ~50-line fetch hook with
  abort-on-unmount and a `reload()` function; the dashboard has no cache
  invalidation graph to manage.

## Not built yet

- No dark/light switch — the theme is dark only (`ui.theme` exists as a setting
  but the UI does not read it yet).
- No file browser for workspace contents beyond the terminal page's listing.
- No streaming of an in-flight command's stdout; a command returns when it
  finishes.
