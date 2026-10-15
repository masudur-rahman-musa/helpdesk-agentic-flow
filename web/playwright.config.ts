import { defineConfig, devices } from "@playwright/test";

/**
 * The e2e run: four processes, one browser, one worker.
 *
 * `workers: 1` and no sharding because flows 4 and 5 destroy state on purpose
 * and `audit_events` takes a per-tenant advisory lock, so two specs mutating
 * at once is a source of flakes rather than of coverage (decision G). The
 * database is reset ONCE before the run by `make e2e`, never per spec: the
 * namespace here is the run.
 *
 * `retries: 0` locally. A retry against a database the first attempt already
 * changed does not re-run the flow, it runs a different one - flow 4's
 * offboard has no second leaver to offboard - so a red here is read, not
 * re-rolled.
 *
 * Four `webServer` entries, each with its OWN readiness URL and a 60 second
 * timeout, so a service that never comes up fails with its own name instead of
 * surfacing as a selector timeout in flow 1 (plan section 5). `/healthz` is
 * process-up for the two Python services; the stub answers the same path
 * because that is what agent-api's `/readyz` probes on the gateway. The web
 * app is probed on `/login`, the one route that needs no session.
 *
 * `reuseExistingServer` is on outside CI so a person iterating on one spec
 * does not pay four cold starts per run.
 */

const BASE_URL = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const STUB_PORT = process.env.E2E_STUB_GATEWAY_PORT ?? "8099";

/** Every server gets the same 60 seconds, named once (plan section 5). */
const SERVER_TIMEOUT_MS = 60_000;

const reuse = !process.env.CI;

export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  reporter: process.env.CI ? "line" : "list",
  timeout: 60_000,
  expect: { timeout: 15_000 },

  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    ...devices["Desktop Chrome"],
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      // The model, and nothing else about the stack (decision C).
      command: "uv run python dev/scripts/e2e/gateway_stub.py",
      url: `http://127.0.0.1:${STUB_PORT}/healthz`,
      cwd: "..",
      timeout: SERVER_TIMEOUT_MS,
      reuseExistingServer: reuse,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: "uv run uvicorn mcp_server.main:app --port 8081",
      url: "http://127.0.0.1:8081/healthz",
      cwd: "..",
      timeout: SERVER_TIMEOUT_MS,
      reuseExistingServer: reuse,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: "uv run uvicorn agent_api.main:app --port 8000",
      url: "http://127.0.0.1:8000/healthz",
      cwd: "..",
      // The one override that makes this a stubbed run: agent-api's model
      // client talks to the canned gateway instead of the real one. Nothing
      // else about the process changes, and no other service is pointed
      // anywhere new (decision C).
      env: { GATEWAY_URL: `http://127.0.0.1:${STUB_PORT}` },
      timeout: SERVER_TIMEOUT_MS,
      reuseExistingServer: reuse,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      // The BUILT app, never `next dev`: the flows should meet the same
      // bundle a reader would deploy, and dev-mode recompiles turn a 200ms
      // navigation into a five second one on first hit of every route.
      command: "pnpm run start",
      url: `${BASE_URL}/login`,
      timeout: SERVER_TIMEOUT_MS,
      reuseExistingServer: reuse,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
