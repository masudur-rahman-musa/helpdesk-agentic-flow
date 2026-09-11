// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as reviewsGET } from "@/app/api/me/reviews/route";
import { POST as decidePOST } from "@/app/api/review-items/[itemId]/decide/route";
import { GET as itemsGET } from "@/app/api/reviews/[reviewId]/items/route";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * An earlier change §6, `reviews-proxy-routes.test.ts`: the review queue, the item read
 * and the decision, proxied, against a stubbed `fetch`. Node environment,
 * because all three handlers are server-side and `NextResponse.redirect` needs
 * the real `Response`.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked exactly as `tests/approvals-proxy-routes.test.ts` mocks it — the
 * modules under test are the three routes, not Next's store.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23h's Deviations):
 * - the queue handler is `export async function GET(request: Request):
 *   Promise<Response>` — it takes a `Request` because decision D has it
 *   forwarding `status` and `limit` off its OWN url, which is the one place
 *   this surface departs from 23b/23d's zero-argument proxies.
 * - the item handler is `export async function GET(request: Request, context:
 *   { params: Promise<{ reviewId: string }> }): Promise<Response>` and the
 *   decision handler `export async function POST(request: Request, context:
 *   { params: Promise<{ itemId: string }> }): Promise<Response>` — Next 15's
 *   async `params`, the segments named `[reviewId]` and `[itemId]` (decision
 *   C: `/api/review-items/…`, never `/api/reviews/items/…`).
 * - the upstream paths are agent-api's own: `/me/reviews`,
 *   `/reviews/<id>/items` and `/review-items/<id>/decide`, never the
 *   browser-facing `/api/...` prefix.
 * - the decision body is forwarded VERBATIM as bytes, with `content-type:
 *   application/json` — the decision word is the TOOL's enum (§5's second
 *   risk), so a proxy that re-parsed and re-serialised would be a second
 *   opinion about a payload it does not validate.
 * - the transport-failure envelope is `502 { code: "UPSTREAM_UNAVAILABLE" }`,
 *   `proxyAgentApi`'s own body, and the 401 answer is a **307** to `/login`,
 *   read off `NextResponse.redirect`'s default status the same way
 *   `tests/approvals-proxy-routes.test.ts` reads it (a project rule: the
 *   redirect is observed on the response, never through `window.location`).
 *
 * No backing service and no page: the only seam is `fetch`.
 */

const TOKEN = "header.payload.signature";
const REVIEW_ID = "55555555-5555-5555-8555-555555555555";
const ITEM_ID = "66666666-6666-5666-8666-666666666666";
const UPSTREAM_ORIGIN = "http://agent-api.test:8000";
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

function queueRequest(query = ""): Request {
  return new Request(`http://web.test/api/me/reviews${query}`, { method: "GET" });
}

function itemsRequest(): Request {
  return new Request(`http://web.test/api/reviews/${REVIEW_ID}/items`, { method: "GET" });
}

function decideRequest(body: unknown): Request {
  return new Request(`http://web.test/api/review-items/${ITEM_ID}/decide`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

const DECISION_BODY = JSON.stringify({ decision: "keep" });

/**
 * The three routes as one table, each reduced to a no-argument call. The
 * refusal cases run over all three, because a guard present on the queue and
 * missing on the decision is exactly the hole a per-route case cannot see.
 */
const routes = [
  {
    name: "/api/me/reviews",
    upstream: "/me/reviews",
    method: "GET",
    body: null as string | null,
    call: () => reviewsGET(queueRequest()),
  },
  {
    name: "/api/reviews/[reviewId]/items",
    upstream: `/reviews/${REVIEW_ID}/items`,
    method: "GET",
    body: null as string | null,
    call: () => itemsGET(itemsRequest(), { params: Promise.resolve({ reviewId: REVIEW_ID }) }),
  },
  {
    name: "/api/review-items/[itemId]/decide",
    upstream: `/review-items/${ITEM_ID}/decide`,
    method: "POST",
    body: DECISION_BODY,
    call: () =>
      decidePOST(decideRequest({ decision: "keep" }), {
        params: Promise.resolve({ itemId: ITEM_ID }),
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
  process.env.AGENT_API_URL = UPSTREAM_ORIGIN;
  fetchStub.mockReset();
  fetchStub.mockResolvedValue(jsonResponse({ reviews: [] }, 200));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete process.env.AGENT_API_URL;
});

describe("the three review routes — the bearer", () => {
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
      expect(String(url)).toBe(`${UPSTREAM_ORIGIN}${upstream}`);
      expect(String(url)).not.toContain(TOKEN);
      expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
    },
  );

  it.each(routes)("$name answers the upstream body unchanged", async ({ call }) => {
    // Catches a route that reshapes or re-wraps what agent-api sent: the page
    // parses the tool's own envelope, so a proxy adding a key of its own
    // leaves `reviews-schema.ts` rejecting a payload agent-api answered
    // correctly.
    const payload = { reviews: [], review_id: REVIEW_ID, item_id: ITEM_ID };
    fetchStub.mockResolvedValue(jsonResponse(payload, 200));

    const response = await call();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(payload);
  });
});

describe("the queue route — its own query parameters (decision D)", () => {
  it("forwards status and limit from its own request url", async () => {
    // Decision D, the one place this surface breaks 23b/23d's zero-argument
    // precedent. Catches a proxy that drops the query — 23e put the status
    // filter on the tool precisely because a completed review is most of the
    // table after two quarters, so a queue that cannot ask for one leaves the
    // page client-filtering through history it never received — and one that
    // renames the parameters on the way through, which agent-api would ignore
    // and answer an unfiltered page for.
    await reviewsGET(queueRequest("?status=completed&limit=25"));

    const [url] = firstCall();
    const forwarded = new URL(String(url));
    expect(forwarded.origin + forwarded.pathname).toBe(`${UPSTREAM_ORIGIN}/me/reviews`);
    expect(forwarded.searchParams.get("status")).toBe("completed");
    expect(forwarded.searchParams.get("limit")).toBe("25");
    expect([...forwarded.searchParams.keys()]).toHaveLength(2);
  });

  it("sends neither parameter when its own url carries neither", async () => {
    // Catches a proxy that always writes both keys: `?status=&limit=` reaches
    // agent-api as an EMPTY status, which the tool's enum refuses, so a page
    // that asked for nothing would get a typed VALIDATION instead of every
    // review — and `limit=` would break the 1..200 bound the same way. Only
    // the presence test in the proxy can flip this row.
    await reviewsGET(queueRequest());

    const [url] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}/me/reviews`);
    expect(String(url)).not.toContain("?");
  });

  it("forwards only the parameter it was given", async () => {
    // The shape between the two above, which neither covers: a status asked
    // for on its own must not drag an empty `limit` along with it. Catches a
    // proxy that reads both parameters and forwards both whatever it found.
    await reviewsGET(queueRequest("?status=open"));

    const [url] = firstCall();
    const forwarded = new URL(String(url));
    expect(forwarded.searchParams.get("status")).toBe("open");
    expect(forwarded.searchParams.has("limit")).toBe(false);
  });
});

describe("the decide route — the body on the wire", () => {
  it("forwards the body verbatim as a POST with its content type", async () => {
    // Catches three proxies that all look right on the two reads: one that
    // forwards a GET (agent-api answers 405 and no decision is ever filed),
    // one that drops the body (agent-api's route then has no `decision` and
    // answers a 422 the page has no sentence for), and one that re-parses and
    // re-serialises the payload, which is a second opinion about a word only
    // the tool validates.
    //
    // The path carries the item id, so this is also what catches a route that
    // ignores its own `params` and posts to a fixed id — the failure that
    // would revoke access on somebody else's item.
    await decidePOST(decideRequest({ decision: "keep" }), {
      params: Promise.resolve({ itemId: ITEM_ID }),
    });

    const [url, init] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}/review-items/${ITEM_ID}/decide`);
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("content-type")).toBe("application/json");
    expect(await sentBody(init)).toBe(DECISION_BODY);
  });

  it("sends no idempotency key of its own", async () => {
    // §1's fourth criterion seen from the browser: the key is DERIVED by
    // agent-api from the verified reviewer, the item and the decision. Catches
    // a proxy that mints one per click — two keys for one double-clicked Keep,
    // which is two decisions filed on one item — and one that forwards a key
    // the browser could choose, which is `IDEMPOTENCY_CONFLICT` taken by
    // accident across two different items.
    await decidePOST(decideRequest({ decision: "keep" }), {
      params: Promise.resolve({ itemId: ITEM_ID }),
    });

    const [url, init] = firstCall();
    expect(await sentBody(init)).not.toContain("idempotency");
    expect(String(url)).not.toContain("idempotency");
    expect(new Headers(init?.headers).get("idempotency-key")).toBeNull();
  });
});

describe("the three review routes — an unreachable agent-api", () => {
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

describe("the three review routes — an expired session", () => {
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
    // it already knows it will get — a round trip, a log line and, on the
    // decide route, a rate-limit slot spent on a mutation that cannot run.
    cookieValue = null;

    const response = await call();

    expect(fetchStub).toHaveBeenCalledTimes(0);
    expect(response.status).toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
  });
});
