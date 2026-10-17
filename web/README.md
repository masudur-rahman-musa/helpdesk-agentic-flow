# Web app

The screens people use: chat, requests, access, approvals, reviews and the
admin pages. Built with Next.js 15, TypeScript, Tailwind and shadcn/ui.

The commands below run from the repo root.

## Folder layout

Everything the app runs is under `src/`.

- `src/app/` holds the pages and routes, the Next.js way.
  - `src/app/login/` is the sign-in page.
  - `src/app/(app)/` holds every signed-in page. They share one layout, and
    one loading, error and not-found screen.
  - `src/app/api/` holds the server routes the browser calls. Each one passes
    the call on to agent-api.
  - `src/app/healthz/` and `src/app/readyz/` answer health checks.
- `src/features/` holds one folder per part of the app. Each keeps its own
  data calls and hooks in an `api.ts`, its screens in `components/`, and its
  data shapes in `schema.ts`. The folders are:
  - `access`: my active access and its end dates.
  - `admin`: the admin pages, one folder each for audit, killswitch, policies
    and rules.
  - `approvals`: the approval queue and its decision details.
  - `auth`: sign in and sign out.
  - `chat`: the chat window, tool steps and citations.
  - `notifications`: in-app notices.
  - `requests`: my requests and their status.
  - `reviews`: access reviews, keep or revoke.
- `src/components/` holds pieces shared by more than one feature.
  - `src/components/ui/` holds small UI pieces: button, card, table, alert and
    the like.
  - `src/components/layout/` holds the app frame: nav, user menu, demo banner,
    theme toggle.
- `src/lib/` holds shared code with no screen of its own.
  - `src/lib/agent-api.ts` is the only file that talks to agent-api.
  - `src/lib/api-paths.ts` lists every path the web calls.
  - `src/lib/query-keys.ts` lists every data cache key.
  - `src/lib/session.ts` reads and writes the login cookie.
  - Date formats, shared labels and error messages live here too.
- `src/middleware.ts` sends anyone without a login cookie to the sign-in page.

Tests sit outside `src/`:

- `tests/` holds the unit tests, run with vitest. Feature tests are in
  `tests/features/`.
- `e2e/` holds the full flows, run in a real browser with Playwright.

## How a request flows

Take a manager opening the Approvals tab.

1. The browser asks the web app for `/api/me/approvals`. It never calls
   agent-api itself.
2. The Next.js route in `src/app/api/me/approvals/` gets the call on the
   server. The paths the browser may use are listed in `src/lib/api-paths.ts`.
3. The route hands it to `proxyAgentApi` in `src/lib/agent-api.ts`. That
   function reads the login token from the cookie, adds it to the call, and
   sets a time limit so a stuck call cannot hang.
4. agent-api checks the token, works out who the person is and which company
   they belong to, and answers. Its answer goes back to the browser as it is.

Why the extra step: the login token sits in an `httpOnly` cookie. No script on
the page can read it, so the browser cannot send it to agent-api on its own.
Only the server side of the web app can. A side effect is that the browser
only ever talks to one address, so there is no cross-site setup to keep.

If agent-api says the token is no good (a 401), the web app clears the cookie
and sends the person back to the sign-in page. If agent-api does not answer,
the browser gets a 502 with the code `UPSTREAM_UNAVAILABLE`.

Chat works the same way, through `src/app/api/chat/stream/`, with a longer
time limit because the answer streams in bit by bit.

## Run it

The web app needs agent-api running. Start the backing services, set up the
database, then start agent-api:

```bash
docker compose up -d
make migrate
make run
```

Seed the demo company once, from `backend/`, with
`uv run python scripts/seed.py`. The sign-in details are in
`docs/demo-accounts.md` at the repo root.

Then start the web app:

```bash
pnpm -C web install
pnpm -C web dev
```

Open http://localhost:3000. Two settings matter:

- `AGENT_API_URL` is where agent-api answers. It defaults to
  `http://localhost:8000`.
- `SESSION_COOKIE_SECURE=false` lets the login cookie work over plain http on
  your own machine. Leave it unset anywhere else.

## Test it

The everyday checks, none of which need a running service:

```bash
pnpm -C web test
pnpm -C web typecheck
pnpm -C web lint
```

`make web-verify` runs all three plus a production build. It is the same check
CI runs.

The full flows run in a real browser and need the whole stack up:

```bash
make e2e
```

That resets the test database, so do not run it against data you want to keep.
