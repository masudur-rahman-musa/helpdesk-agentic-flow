// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as searchGET } from "@/app/api/admin/audit/search/route";
import { GET as verifyGET } from "@/app/api/admin/audit/verify/route";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * `audit-proxy-routes.test.ts`: the audit search and the chain
 * verification, proxied, against a stubbed `fetch`. Node environment, because
 * both handlers are server-side and `NextResponse.redirect` needs the real
 * `Response`.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked exactly as `tests/reviews-proxy-routes.test.ts` mocks it — the
 * modules under test are the two routes, not Next's store.
 *
 * Shapes this file fixes:
 * - the search handler is `export async function GET(request: Request):
 *   Promise<Response>` — it takes a `Request` because it
 *   forwards all eight filters off its OWN url — and the verify handler is
 *   `export async function GET(): Promise<Response>`, the zero-argument
 *   shape, because the route takes no parameter at all.
 * - the upstream paths are agent-api's own, `/admin/audit/search` and
 *   `/admin/audit/verify`, never the browser-facing `/api/...` prefix.
 * - the transport-failure envelope is `502 { code: "UPSTREAM_UNAVAILABLE" }`,
 *   `proxyAgentApi`'s own body, and the 401 answer is a **307** to `/login`,
 *   read off `NextResponse.redirect`'s default status the same way
 *   `tests/reviews-proxy-routes.test.ts` reads it (the
 *   redirect is observed on the response's status and `location` header,
 *   never through `window.location`).
 * - the eight forwarded parameter names are the tool's own, in the order the
 *   tool lists them, and each is sent only when present.
 *
 * No backing service and no page: the only seam is `fetch`.
 */

const TOKEN = "header.payload.signature";
const REQUEST_ID = "55555555-5555-5555-8555-555555555555";
const UPSTREAM_ORIGIN = "http://agent-api.test:8000";
const REDIRECT_STATUS = 307;
const UPSTREAM_UNAVAILABLE_STATUS = 502;
const FORBIDDEN_STATUS = 403;

/** The eight filters, with a value for each. */
const FILTERS: ReadonlyArray<readonly [string, string]> = [
  ["event_type", "access_request.created"],
  ["subject_type", "access_request"],
  ["subject_id", REQUEST_ID],
  ["actor_ref", "fake.person@example.invalid"],
  ["occurred_from", "2026-01-01T00:00:00Z"],
  ["occurred_to", "2026-02-01T00:00:00Z"],
  ["before_seq", "900"],
  ["limit", "25"],
];

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

function searchRequest(query = ""): Request {
  return new Request(`http://web.test/api/admin/audit/search${query}`, { method: "GET" });
}

/**
 * The two routes as one table, each reduced to a no-argument call. The refusal
 * cases run over both, because a guard present on the search and missing on
 * the verify is exactly the hole a per-route case cannot see.
 */
const routes = [
  {
    name: "/api/admin/audit/search",
    upstream: "/admin/audit/search",
    call: () => searchGET(searchRequest()),
  },
  {
    name: "/api/admin/audit/verify",
    upstream: "/admin/audit/verify",
    call: () => verifyGET(),
  },
] as const;

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
  fetchStub.mockResolvedValue(jsonResponse({ events: [], next_before_seq: null }, 200));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete process.env.AGENT_API_URL;
});

describe("the two audit routes — the bearer", () => {
  it.each(routes)(
    "$name sends the bearer from the httpOnly cookie and never in the URL",
    async ({ call, upstream }) => {
      // Catches a route that reads a token from a header the browser set or
      // from the query string — the cookie is the only copy of the bearer, so anything else means
      // the client was handed one,
      // and a token in a URL lands in every access log. Also catches a route
      // that calls agent-api directly instead of through `proxyAgentApi`: an
      // unrouted call carries no authorization header at all, and on THIS
      // surface that is the whole audit log answered without a principal.
      await call();

      const [url, init] = firstCall();
      expect(String(url)).toBe(`${UPSTREAM_ORIGIN}${upstream}`);
      expect(String(url)).not.toContain(TOKEN);
      expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
    },
  );

  it.each(routes)("$name answers the upstream body unchanged", async ({ call }) => {
    // Catches a route that reshapes or re-wraps what agent-api sent: the page
    // parses the tool's own envelope, so a proxy adding a key of its own
    // leaves `audit-schema.ts` rejecting a payload agent-api answered
    // correctly. On an audit surface a proxy that filtered rows would also be
    // a second, invisible scope filter over the one the tool already applies.
    const payload = { events: [], next_before_seq: null, ok: true, broken_at: null };
    fetchStub.mockResolvedValue(jsonResponse(payload, 200));

    const response = await call();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(payload);
  });
});

describe("the search route — its own query parameters (decision D)", () => {
  it("forwards all eight filters from its own request url", async () => {
    // Catches a proxy that drops the query. The tool takes these eight
    // filters because an audit log is unusable without them, and
    // this route is the search box's only caller, so a dropped query leaves
    // the page filtering client-side over one page of fifty rows — and one
    // that renames a parameter on the way through, which agent-api would
    // ignore while answering an unfiltered page that LOOKS like a result.
    const query = FILTERS.map(([name, value]) => `${name}=${encodeURIComponent(value)}`).join("&");

    await searchGET(searchRequest(`?${query}`));

    const [url] = firstCall();
    const forwarded = new URL(String(url));
    expect(forwarded.origin + forwarded.pathname).toBe(`${UPSTREAM_ORIGIN}/admin/audit/search`);
    for (const [name, value] of FILTERS) {
      expect(forwarded.searchParams.get(name)).toBe(value);
    }
    expect([...forwarded.searchParams.keys()]).toHaveLength(FILTERS.length);
    expect(FILTERS).toHaveLength(8);
  });

  it("sends no parameter at all when its own url carries none", async () => {
    // Catches a proxy that always writes all eight keys: `?event_type=` reaches
    // agent-api as an EMPTY filter, `?limit=` breaks the tool's 1-200 bound and
    // `?occurred_from=` is not a date, so a page that asked for nothing would
    // get a typed VALIDATION instead of the newest fifty events. Only the
    // presence test in the proxy can flip this row.
    await searchGET(searchRequest());

    const [url] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}/admin/audit/search`);
    expect(String(url)).not.toContain("?");
  });

  it.each(FILTERS)("forwards %s alone when it is the only one given", async (name, value) => {
    // The eight shapes between "none" and "all eight", which neither case
    // above covers: one filter asked for must not drag seven empty ones along
    // with it. Catches a proxy that reads every parameter and forwards
    // whatever it found, and one that forwards a hardcoded subset — the four
    // filters a first draft remembers, with `actor_ref` or `before_seq` (the
    // cursor, so paging) silently unreachable.
    await searchGET(searchRequest(`?${name}=${encodeURIComponent(value)}`));

    const [url] = firstCall();
    const forwarded = new URL(String(url));
    expect(forwarded.searchParams.get(name)).toBe(value);
    expect([...forwarded.searchParams.keys()]).toEqual([name]);
  });
});

describe("the verify route — its own shape", () => {
  it("forwards no query and no body", async () => {
    // the route takes nothing: the tenant comes from the token.
    // Catches a proxy that passes a tenant, a user or a filter of its
    // own along — on the one route in this diff that reads the database
    // directly rather than through a tool, an argument the browser could set
    // is the whole chain answered for somebody else's tenant.
    await verifyGET();

    const [url, init] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}/admin/audit/verify`);
    expect(init?.body ?? null).toBeNull();
    expect(init?.method).toBe("GET");
  });

  it("forwards a 403 with its typed body instead of flattening it into a 502", async () => {
    // the route answers 403 to
    // a VERIFIED token without an audit principal. Catches a proxy that treats
    // every non-200 as a transport failure: an ordinary employee would be told
    // the chain is unreachable — which reads as an outage, or as tampering —
    // rather than that they may not ask. The status and the typed body both
    // have to survive, because the page renders one sentence per typed error code.
    //
    // Only the non-200 handling can decide this row: the cookie is present,
    // the fetch resolves, and 401 is the sole status `proxyAgentApi` acts on.
    fetchStub.mockResolvedValue(jsonResponse({ code: "FORBIDDEN" }, FORBIDDEN_STATUS));

    const response = await verifyGET();

    expect(response.status).toBe(FORBIDDEN_STATUS);
    expect(await response.json()).toEqual({ code: "FORBIDDEN" });
  });
});

describe("the two audit routes — an unreachable agent-api", () => {
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

describe("the two audit routes — an expired session", () => {
  it.each(routes)(
    "$name answers a 307 to /login and expires the cookie on a 401",
    async ({ call }) => {
      // Catches a route that passes agent-api's 401 through to the browser: the
      // stale cookie survives, so every later call fails the same way with
      // nothing telling the user why. This is the `proxyAgentApi` seam, so a
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
    // it already knows it will get — a round trip and a log line per load, on
    // a surface a page polls.
    cookieValue = null;

    const response = await call();

    expect(fetchStub).toHaveBeenCalledTimes(0);
    expect(response.status).toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
  });
});
