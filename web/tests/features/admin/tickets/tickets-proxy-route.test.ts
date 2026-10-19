// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as ticketsGET } from "@/app/api/admin/tickets/route";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * `tickets-proxy-route.test.ts`: the open tickets board, proxied, against a
 * stubbed `fetch`. Node environment, because the handler is server-side and
 * `NextResponse.redirect` needs the real `Response`.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked exactly as `rules-proxy-routes.test.ts` mocks it.
 *
 * Shapes this file fixes:
 * - the handler is `export async function GET(request: Request):
 *   Promise<Response>`, because the agent-api route takes an optional `limit`
 *   and the search string must be passed on.
 * - the upstream path is agent-api's own `/admin/tickets`, never the
 *   browser-facing `/api/...` prefix.
 * - the 401 answer is a 307 to `/login` with the session cookie expired
 *   (`proxyAgentApi`'s own behaviour), the transport failure is
 *   `502 { code: "UPSTREAM_UNAVAILABLE" }`.
 */

const TOKEN = "header.payload.signature";
const UPSTREAM_ORIGIN = "http://agent-api.test:8000";
const UPSTREAM_PATH = "/admin/tickets";
const REDIRECT_STATUS = 307;
const UPSTREAM_UNAVAILABLE_STATUS = 502;

const BOARD_BODY = {
  tickets: [
    {
      ticket_id: "88888888-8888-5888-8888-888888888888",
      ticket_number: "TCK-0042",
      kind: "general",
      title: "Obviously fake ticket title.",
      opened_by: "Fake Person One",
      created_at: "2026-06-02T10:15:00Z",
    },
  ],
};

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

function request(search = ""): Request {
  return new Request(`http://web.test/api/admin/tickets${search}`, { method: "GET" });
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
  process.env.AGENT_API_URL = UPSTREAM_ORIGIN;
  fetchStub.mockReset();
  fetchStub.mockResolvedValue(jsonResponse(BOARD_BODY, 200));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete process.env.AGENT_API_URL;
});

describe("the open tickets proxy", () => {
  it("/api/admin/tickets proxies GET to /admin/tickets", async () => {
    // Kill: catches a route that calls the wrong upstream path, one that
    // calls agent-api directly instead of through `proxyAgentApi` (no bearer
    // header), and one that puts the token in the URL. The bearer comes from
    // the httpOnly cookie, and the answer is the upstream body unchanged.
    const response = await ticketsGET(request());

    const [url, init] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}${UPSTREAM_PATH}`);
    expect(String(url)).not.toContain(TOKEN);
    expect(init?.method).toBe("GET");
    expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(BOARD_BODY);
  });

  it("passes the search string on", async () => {
    // Kill: catches a route that drops the query (the page size the admin
    // asked for is lost), and one that rewrites it. `limit=5` is the only
    // thing the agent-api route reads.
    await ticketsGET(request("?limit=5"));

    const [url] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}${UPSTREAM_PATH}?limit=5`);
  });

  it("sends no question mark when the browser sent no query", async () => {
    // Kill: catches a route that always appends `?`, which the upstream would
    // read as an empty `limit=`-style input.
    await ticketsGET(request());

    const [url] = firstCall();
    expect(String(url)).not.toContain("?");
  });

  it("answers 502 UPSTREAM_UNAVAILABLE when the call throws", async () => {
    // Kill: catches a route whose `fetch` rejection is left to propagate (an
    // untyped 500 with a stack naming the internal host).
    fetchStub.mockRejectedValue(new TypeError("fetch failed"));

    const response = await ticketsGET(request());

    expect(response.status).toBe(UPSTREAM_UNAVAILABLE_STATUS);
    expect(await response.json()).toEqual({ code: "UPSTREAM_UNAVAILABLE" });
  });

  it("expires the session cookie and redirects to /login on a 401", async () => {
    // Kill: catches a route that passes agent-api's 401 through to the
    // browser, so the stale cookie survives. An unrouted `fetch` answers 401
    // and clears no cookie, so this also catches a route that bypasses
    // `proxyAgentApi`.
    fetchStub.mockResolvedValue(jsonResponse({ code: "FORBIDDEN" }, 401));

    const response = await ticketsGET(request());

    expect(response.status).toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
    const [cookie] = response.headers.getSetCookie();
    expect(cookie?.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
  });
});
