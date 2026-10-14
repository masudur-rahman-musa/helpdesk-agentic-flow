// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST as approvePOST } from "@/app/api/approvals/[requestId]/approve/route";
import { POST as rejectPOST } from "@/app/api/approvals/[requestId]/reject/route";
import { GET as approvalsGET } from "@/app/api/me/approvals/route";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * An earlier change §6, `approvals-proxy-routes.test.ts`: `GET /api/me/approvals` and
 * the two decision proxies against a stubbed `fetch`. Node environment,
 * because all three handlers are server-side and `NextResponse.redirect`
 * needs the real `Response`.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked exactly as `tests/me-proxy-routes.test.ts` mocks it — the modules
 * under test are the three routes, not Next's store.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23c's Deviations):
 * - the queue handler is `export async function GET(): Promise<Response>`, the
 *   zero-argument shape the `/api/me` proxies already have. The `limit` filter
 *   lives on the agent-api route (§1) and no page exists yet to send one.
 * - each decision handler is
 *   `export async function POST(request: Request, context: { params:
 *   Promise<{ requestId: string }> }): Promise<Response>` — Next 15's async
 *   `params`, the segment named `[requestId]`.
 * - the upstream paths are agent-api's own: `/me/approvals`,
 *   `/approvals/<id>/approve` and `/approvals/<id>/reject`, never the
 *   browser-facing `/api/...` prefix.
 * - the decision body is forwarded VERBATIM as bytes, with
 *   `content-type: application/json` — the comment and reason bounds are the
 *   TOOL's (decision E), so a proxy that re-parsed and re-serialised would be
 *   a second opinion about a payload it does not validate.
 * - the transport-failure envelope is `502 { code: "UPSTREAM_UNAVAILABLE" }`,
 *   `proxyAgentApi`'s own body, and the 401 answer is a **307** to `/login`,
 *   read off `NextResponse.redirect`'s default status the same way
 *   `tests/me-proxy-routes.test.ts` reads it.
 *
 * No backing service and no page: the only seam is `fetch`.
 */

const TOKEN = "header.payload.signature";
const REQUEST_ID = "66666666-6666-5666-8666-666666666666";
const REDIRECT_STATUS = 307;
const UPSTREAM_UNAVAILABLE_STATUS = 502;

let cookieValue: string | null = TOKEN;

vi.mock("next/headers", () => ({
  cookies: async () =>
    Promise.resolve({
      get: (name: string) =>
        name === SESSION_COOKIE_NAME && cookieValue !== null
          ? { name, value: cookieValue }
          : undefined,
    }),
}));

const fetchStub = vi.fn<typeof fetch>();

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function decisionRequest(verb: string, body: unknown): Request {
  return new Request(`http://web.test/api/approvals/${REQUEST_ID}/${verb}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

/**
 * The three routes as one table, each reduced to a no-argument call. The
 * refusal cases run over all three, because a guard present on the queue and
 * missing on a decision is exactly the hole a per-route case cannot see.
 */
const routes = [
  {
    name: "/api/me/approvals",
    upstream: "/me/approvals",
    method: "GET",
    body: null as string | null,
    call: () => approvalsGET(),
  },
  {
    name: "/api/approvals/[requestId]/approve",
    upstream: `/approvals/${REQUEST_ID}/approve`,
    method: "POST",
    body: JSON.stringify({ comment: "Obviously fake comment." }),
    call: () =>
      approvePOST(decisionRequest("approve", { comment: "Obviously fake comment." }), {
        params: Promise.resolve({ requestId: REQUEST_ID }),
      }),
  },
  {
    name: "/api/approvals/[requestId]/reject",
    upstream: `/approvals/${REQUEST_ID}/reject`,
    method: "POST",
    body: JSON.stringify({ reason: "Obviously fake reason." }),
    call: () =>
      rejectPOST(decisionRequest("reject", { reason: "Obviously fake reason." }), {
        params: Promise.resolve({ requestId: REQUEST_ID }),
      }),
  },
] as const;

function firstCall(): [RequestInfo | URL, RequestInit | undefined] {
  const call = fetchStub.mock.calls[0];
  if (call === undefined) {
    throw new Error("expected agent-api to be called");
  }
  return [call[0], call[1]];
}

async function sentBody(init: RequestInit | undefined): Promise<string> {
  const body = init?.body;
  if (body === undefined || body === null) {
    return "";
  }
  return await new Response(body as BodyInit).text();
}

beforeEach(() => {
  cookieValue = TOKEN;
  process.env.AGENT_API_URL = "http://agent-api.test:8000";
  fetchStub.mockReset();
  fetchStub.mockResolvedValue(jsonResponse({ approvals: [] }, 200));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete process.env.AGENT_API_URL;
});

describe("the three approval routes — the bearer", () => {
  it.each(routes)(
    "$name sends the bearer from the httpOnly cookie and never in the URL",
    async ({ call, upstream }) => {
      // Catches a route that reads a token from a header the browser set or
      // from the query string — the cookie is the only copy of the bearer
      // (22a decision A), so anything else means the client was handed one,
      // and a token in a URL lands in every access log. Also catches a route
      // that calls agent-api directly instead of through `proxyAgentApi`: an
      // unrouted call carries no authorization header at all.
      await call();

      const [url, init] = firstCall();
      expect(String(url)).toBe(`http://agent-api.test:8000${upstream}`);
      expect(String(url)).not.toContain(TOKEN);
      expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
    },
  );

  it.each(routes)("$name answers the upstream body unchanged", async ({ call }) => {
    // Catches a route that reshapes or re-wraps what agent-api sent: the page
    // parses the tool's own envelope, so a proxy adding a key of its own
    // leaves the parser rejecting a payload agent-api answered correctly.
    const payload = { approvals: [], request_id: REQUEST_ID, status: "approved" };
    fetchStub.mockResolvedValue(jsonResponse(payload, 200));

    const response = await call();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(payload);
  });
});

describe("the two decision routes — the body on the wire", () => {
  const decisions = routes.filter((route) => route.method === "POST");

  it.each(decisions)(
    "$name forwards the body verbatim as a POST with its content type",
    async ({ call, body, upstream }) => {
      // Catches three proxies that all look right on the queue route: one that
      // forwards a GET (agent-api answers 405 and no decision is ever filed),
      // one that drops the body (the tool refuses a missing `reason`, and an
      // approver's comment is lost from the audit trail), and one that
      // re-parses and re-serialises the payload, which is a second opinion
      // about a body only the tool validates (decision E).
      //
      // The path carries the request id, so this is also what catches a route
      // that ignores its own `params` and posts to a fixed id.
      await call();

      const [url, init] = firstCall();
      expect(String(url)).toBe(`http://agent-api.test:8000${upstream}`);
      expect(init?.method).toBe("POST");
      expect(new Headers(init?.headers).get("content-type")).toBe("application/json");
      expect(await sentBody(init)).toBe(body);
    },
  );
});

describe("the three approval routes — an unreachable agent-api", () => {
  it.each(routes)(
    "$name answers 502 UPSTREAM_UNAVAILABLE when the call throws",
    async ({ call }) => {
      // Catches a route whose `fetch` rejection is left to propagate: Next
      // answers an untyped 500 with a stack, which the page cannot map to any
      // sentence and which names the internal host. Only the transport branch
      // can produce this row — the cookie is present and the token is never
      // rejected here.
      fetchStub.mockRejectedValue(new TypeError("fetch failed"));

      const response = await call();

      expect(response.status).toBe(UPSTREAM_UNAVAILABLE_STATUS);
      expect(await response.json()).toEqual({ code: "UPSTREAM_UNAVAILABLE" });
    },
  );
});

describe("the three approval routes — an expired session", () => {
  it.each(routes)(
    "$name answers a 307 to /login and expires the cookie on a 401",
    async ({ call }) => {
      // Catches a route that passes agent-api's 401 through to the browser: the
      // stale cookie survives, so every later call fails the same way with
      // nothing telling the user why. This is 22a's `proxyAgentApi` seam, so a
      // route that opens its own `fetch` is caught here too — an unrouted call
      // answers 401, not a redirect, and clears no cookie.
      fetchStub.mockResolvedValue(jsonResponse({ code: "FORBIDDEN" }, 401));

      const response = await call();

      expect(response.status).toBe(REDIRECT_STATUS);
      expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
      const cookies = response.headers.getSetCookie();
      expect(cookies).toHaveLength(1);
      const [cookie] = cookies;
      expect(cookie?.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
      expect(/Max-Age=0|Expires=Thu, 01 Jan 1970/.test(cookie ?? "")).toBe(true);
    },
  );

  it.each(routes)("$name redirects without calling agent-api with no cookie", async ({ call }) => {
    // Catches a route that sends `Bearer null` upstream and waits for the 401
    // it already knows it will get — a round trip, a log line and, on the two
    // decision routes, a rate-limit slot spent on a mutation that cannot run.
    cookieValue = null;

    const response = await call();

    expect(fetchStub).toHaveBeenCalledTimes(0);
    expect(response.status).toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
  });
});
