// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as accessGET } from "@/app/api/me/access/route";
import { GET as meGET } from "@/app/api/me/route";
import { GET as requestsGET } from "@/app/api/me/requests/route";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * `me-proxy-routes.test.ts`: `GET /api/me/requests` and
 * `GET /api/me/access` against a stubbed `fetch`. Node environment, because
 * both handlers are server-side and `NextResponse.redirect` needs the real
 * `Response`.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked exactly as `tests/agent-api-proxy.test.ts` and
 * `tests/chat-stream-route.test.ts` mock it — the modules under test are the
 * two routes, not Next's store.
 *
 * Shapes this file fixes:
 * - both handlers are `export async function GET(): Promise<Response>`, the
 *   zero-argument shape `app/api/chat/messages/route.ts` already has. The
 *   `status` and `limit` filters live on the agent-api route and no page
 *   exists yet to send them; the signature widens if a page needs to.
 * - the upstream paths are `/me/requests` and `/me/access` — agent-api's own,
 *   not the browser-facing `/api/...` prefix.
 * - the transport-failure envelope is `502 { code: "UPSTREAM_UNAVAILABLE" }`,
 *   which is `proxyAgentApi`'s own body, and the 401 answer is a **307** to
 *   `/login`, read off `NextResponse.redirect`'s default status the same way
 *   `tests/chat-stream-route.test.ts` reads it.
 *
 * No backing service and no page: the only seam is `fetch`.
 */

const TOKEN = "header.payload.signature";
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

const routes = [
  { name: "/api/me/requests", handler: requestsGET, upstream: "/me/requests" },
  { name: "/api/me/access", handler: accessGET, upstream: "/me/access" },
  // The shell's own read. The upstream URL is compared whole, so a
  // query string or a path suffix on the proxied call fails the bearer row.
  { name: "/api/me", handler: meGET, upstream: "/me" },
] as const;

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function firstCall(): [RequestInfo | URL, RequestInit | undefined] {
  const call = fetchStub.mock.calls[0];
  if (call === undefined) {
    throw new Error("expected agent-api to be called");
  }
  return [call[0], call[1]];
}

beforeEach(() => {
  cookieValue = TOKEN;
  process.env.AGENT_API_URL = "http://agent-api.test:8000";
  fetchStub.mockReset();
  fetchStub.mockResolvedValue(jsonResponse({ requests: [] }, 200));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete process.env.AGENT_API_URL;
});

describe("the two /api/me routes — the bearer", () => {
  it.each(routes)(
    "$name sends the bearer from the httpOnly cookie and never in the URL",
    async ({ handler, upstream }) => {
      // Catches a route that reads a token from a header the browser set or
      // from the query string — the cookie is the only copy of the bearer, so anything else means
      // the client was handed one,
      // and a token in a URL lands in every access log. Also catches a route
      // that calls agent-api directly instead of through `proxyAgentApi`: an
      // unrouted call carries no authorization header at all.
      await handler();

      const [url, init] = firstCall();
      expect(String(url)).toBe(`http://agent-api.test:8000${upstream}`);
      expect(String(url)).not.toContain(TOKEN);
      expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
    },
  );

  it.each(routes)("$name answers the upstream body unchanged", async ({ handler }) => {
    // Catches a route that reshapes or re-wraps what agent-api sent: the page
    // parses the tool's own envelope (`{ requests: [...] }` /
    // `{ grants: [...] }`), so a proxy adding a key of its own leaves the
    // parser rejecting a payload agent-api answered correctly.
    const payload = { requests: [], grants: [] };
    fetchStub.mockResolvedValue(jsonResponse(payload, 200));

    const response = await handler();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(payload);
  });
});

describe("the two /api/me routes — an unreachable agent-api", () => {
  it.each(routes)(
    "$name answers 502 UPSTREAM_UNAVAILABLE when the call throws",
    async ({ handler }) => {
      // Catches a route whose `fetch` rejection is left to propagate: Next
      // answers an untyped 500 with a stack, which the page cannot map to any
      // sentence and which names the internal host. Only the transport branch
      // can produce this row — the cookie is present and the token is never
      // rejected here.
      fetchStub.mockRejectedValue(new TypeError("fetch failed"));

      const response = await handler();

      expect(response.status).toBe(UPSTREAM_UNAVAILABLE_STATUS);
      expect(await response.json()).toEqual({ code: "UPSTREAM_UNAVAILABLE" });
    },
  );
});

describe("the two /api/me routes — an expired session", () => {
  it.each(routes)(
    "$name answers a 307 to /login and expires the cookie on a 401",
    async ({ handler }) => {
      // Catches a route that passes agent-api's 401 through to the browser:
      // the stale cookie survives, so every later call fails the same way with
      // nothing telling the user why. This is the `proxyAgentApi` seam, so a
      // route that opens its own `fetch` is caught here too — an unrouted call
      // answers 401, not a redirect, and clears no cookie.
      fetchStub.mockResolvedValue(jsonResponse({ code: "FORBIDDEN" }, 401));

      const response = await handler();

      expect(response.status).toBe(REDIRECT_STATUS);
      expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
      const cookies = response.headers.getSetCookie();
      expect(cookies).toHaveLength(1);
      const [cookie] = cookies;
      expect(cookie?.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
      expect(/Max-Age=0|Expires=Thu, 01 Jan 1970/.test(cookie ?? "")).toBe(true);
    },
  );

  it.each(routes)(
    "$name redirects without calling agent-api with no cookie",
    async ({ handler }) => {
      // Catches a route that sends `Bearer null` upstream and waits for the 401
      // it already knows it will get — a round trip, a log line and a rate-limit
      // slot spent on a request that cannot succeed.
      cookieValue = null;

      const response = await handler();

      expect(fetchStub).toHaveBeenCalledTimes(0);
      expect(response.status).toBe(REDIRECT_STATUS);
      expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
    },
  );
});
