// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as healthzGET } from "@/app/healthz/route";
import { GET as readyzGET } from "@/app/readyz/route";
import { READYZ_TIMEOUT_MS } from "@/lib/readyz-timeout";

/**
 * An earlier change §6, `health-routes.test.ts`: the web container's two probe routes
 * against a stubbed `fetch`. Node environment, not jsdom, because both handlers
 * are server-side and answer with a real `Response` (a project rule: jsdom's
 * globals are not the runtime these run in).
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * they belong in 30a's Deviations):
 * - both handlers are `export async function GET(): Promise<Response>`, the
 *   zero-argument shape the `/api/me/*` routes already have.
 * - the bodies are JSON with a single key, `status`: `{"status":"ok"}` from
 *   `/healthz`, `{"status":"ready"}` from a 200 `/readyz`, and
 *   `{"status":"unready"}` from a 503. No upstream body, no URL and no error
 *   text is forwarded (criterion: the body names a status only).
 * - `/readyz` asks agent-api's own `/readyz`, at `${AGENT_API_URL}/readyz`,
 *   with `AGENT_API_URL` read per call the way `lib/agent-api.ts` reads it.
 * - the readiness fetch carries `AbortSignal.timeout(READYZ_TIMEOUT_MS)` with
 *   `READYZ_TIMEOUT_MS` exported at exactly 2000. It
 *   is imported from `lib/readyz-timeout.ts`, not from the route: Next refuses
 *   any export from a route module but its handlers and config.
 *
 * No backing service: the only seam is `fetch`, stubbed exactly as
 * `tests/agent-api.test.ts` stubs it.
 */

const AGENT_API_BASE = "http://agent-api.test:8000";
const EXPECTED_READYZ_TIMEOUT_MS = 2_000;
const READY_STATUS = 200;
const UNREADY_STATUS = 503;

const fetchStub = vi.fn<typeof fetch>();

function upstream(status: number): Response {
  return new Response(JSON.stringify({ status: "ok" }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  process.env.AGENT_API_URL = AGENT_API_BASE;
  fetchStub.mockReset();
  fetchStub.mockResolvedValue(upstream(200));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_API_URL;
});

describe("web health routes", () => {
  it("answers healthz with 200 when nothing else is reachable", async () => {
    // Kills a /healthz that checks a dependency: liveness answers whether this
    // process is up, so a probe that fails when agent-api is down gets the web
    // container RESTARTED for someone else's outage. The rejecting stub plus
    // the not-called assertion is what separates it from /readyz.
    fetchStub.mockRejectedValue(new Error("ECONNREFUSED"));

    const response = await healthzGET();

    expect(response.status).toBe(READY_STATUS);
    expect(await response.json()).toEqual({ status: "ok" });
    expect(fetchStub).not.toHaveBeenCalled();
  });

  it("answers readyz with 200 when agent-api answers", async () => {
    // Kills a /readyz wired to the wrong upstream path or base URL: it would
    // answer 503 forever with agent-api healthy, and the pod would never take
    // traffic. Asserts the URL it asked for, not only the status.
    const response = await readyzGET();

    expect(response.status).toBe(READY_STATUS);
    expect(await response.json()).toEqual({ status: "ready" });
    expect(fetchStub.mock.calls[0]?.[0]).toBe(`${AGENT_API_BASE}/readyz`);
  });

  it("answers readyz with 503 when agent-api refuses the connection", async () => {
    // Kills the static 200 decision C2 rejected, and kills an unguarded fetch
    // whose rejection escapes as a 500: a readiness probe must read the refusal
    // as not-ready, which is the one signal that stops traffic reaching a web
    // pod whose only upstream is down.
    fetchStub.mockRejectedValue(new Error("ECONNREFUSED"));

    const response = await readyzGET();

    expect(response.status).toBe(UNREADY_STATUS);
    expect(await response.json()).toEqual({ status: "unready" });
  });

  it("answers readyz with 503 when agent-api answers a non-2xx status", async () => {
    // Kills a handler that only catches the throw: agent-api answering 503 from
    // its own readiness check is reachable, so a `try/catch` with no
    // `response.ok` test reports ready while the upstream says it is not.
    fetchStub.mockResolvedValue(upstream(503));

    const response = await readyzGET();

    expect(response.status).toBe(UNREADY_STATUS);
    expect(await response.json()).toEqual({ status: "unready" });
  });

  it("never puts the agent-api url in the readyz body", async () => {
    // Kills a handler that forwards the upstream body or the caught error into
    // its own: both carry `http://agent-api.test:8000`, which publishes the
    // internal service address to anyone who can reach the probe path.
    fetchStub.mockRejectedValue(new Error(`connect ECONNREFUSED ${AGENT_API_BASE}/readyz`));
    const refused = await readyzGET();
    const refusedBody = await refused.text();

    expect(refusedBody).not.toContain("agent-api.test");
    expect(refusedBody).not.toContain("8000");
    expect(JSON.parse(refusedBody)).toEqual({ status: "unready" });

    fetchStub.mockResolvedValue(
      new Response(JSON.stringify({ upstream: `${AGENT_API_BASE}/readyz` }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const ready = await readyzGET();
    const readyBody = await ready.text();

    expect(readyBody).not.toContain("agent-api.test");
    expect(JSON.parse(readyBody)).toEqual({ status: "ready" });
  });

  it("pins the readyz timeout at exactly 2000ms and carries its signal on the outgoing call", async () => {
    // a project rule and 11, which §6 named no case for (30a Deviations).
    // Kills a probe with no timeout at all: a hung agent-api socket holds the
    // readiness request open past the kubelet's own timeout, so the probe fails
    // by timing out rather than by answering, and kills equally a constant that
    // exists and is never passed to fetch.
    await readyzGET();

    const init = fetchStub.mock.calls[0]?.[1];
    expect(READYZ_TIMEOUT_MS).toBe(EXPECTED_READYZ_TIMEOUT_MS);
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });
});
