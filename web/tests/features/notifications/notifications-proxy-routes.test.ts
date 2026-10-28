// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as notificationsGET } from "@/app/api/me/notifications/route";
import { POST as readPOST } from "@/app/api/notifications/[notificationId]/read/route";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * `notifications-proxy-routes.test.ts`: the page read and
 * the read stamp, proxied, against a stubbed `fetch`. Node environment, because
 * both handlers are server-side and `NextResponse.redirect` needs the real
 * `Response`.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked exactly as `tests/reviews-proxy-routes.test.ts` mocks it -- the
 * modules under test are the two routes, not Next's store.
 *
 * Shapes this file fixes:
 * - the page handler is `export async function GET(request: Request):
 *   Promise<Response>` -- it takes a `Request` because it
 *   forwards `unread_only` and `limit` off its OWN url, the shape.
 * - the stamp handler is `export async function POST(request: Request, context:
 *   { params: Promise<{ notificationId: string }> }): Promise<Response>`,
 *   Next 15's async `params` over a `[notificationId]` segment, which is the
 *   signature the tool states.
 * - the upstream paths are agent-api's own: `/me/notifications` and
 *   `/notifications/<id>/read`, never the browser-facing `/api/...` prefix.
 * - the transport-failure envelope is `502 { code: "UPSTREAM_UNAVAILABLE" }`,
 *   `proxyAgentApi`'s own body.
 * - the 401 answer is observed as a **307** to `/login` with a cleared cookie,
 *   read off `NextResponse.redirect`'s default status the way
 *   `reviews-proxy-routes.test.ts` and
 *   `approvals-proxy-routes.test.ts` already read it. Observing it through
 *   `redirected`/`url` is not possible HERE: that applies only to a
 *   case that FOLLOWS a redirect, and what these handlers return is the
 *   unfollowed `NextResponse.redirect` itself, whose `redirected` is `false`
 *   and whose `url` is empty. The status and the `location` header are the only
 *   things there are to assert, and no `window.location` is touched by any case
 *   here -- this file runs in the node environment and stubs only `fetch`,
 *   through `vi.stubGlobal`.
 *
 * No backing service and no page: the only seam is `fetch`.
 */

const TOKEN = "header.payload.signature";
const NOTIFICATION_ID = "99999999-9999-5999-8999-999999999999";
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

function pageRequest(query = ""): Request {
  return new Request(`http://web.test/api/me/notifications${query}`, { method: "GET" });
}

function stampRequest(): Request {
  return new Request(`http://web.test/api/notifications/${NOTIFICATION_ID}/read`, {
    method: "POST",
  });
}

/**
 * The two routes as one table, each reduced to a no-argument call. The refusal
 * cases run over both, because a guard present on the read and missing on the
 * mutation is exactly the hole a per-route case cannot see.
 */
const routes = [
  {
    name: "/api/me/notifications",
    upstream: "/me/notifications",
    call: () => notificationsGET(pageRequest()),
  },
  {
    name: "/api/notifications/[notificationId]/read",
    upstream: `/notifications/${NOTIFICATION_ID}/read`,
    call: () =>
      readPOST(stampRequest(), {
        params: Promise.resolve({ notificationId: NOTIFICATION_ID }),
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
  fetchStub.mockResolvedValue(jsonResponse({ notifications: [], unread_count: 0 }, 200));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete process.env.AGENT_API_URL;
});

describe("the two notification routes - the bearer", () => {
  it.each(routes)(
    "$name sends the bearer from the httpOnly cookie and never in the URL",
    async ({ call, upstream }) => {
      // Catches a route that reads a token from a header the browser set or
      // from the query string -- the cookie is the only copy of the bearer, so anything else means
      // the client was handed one,
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
    // parses the tool's own envelope, so a proxy adding a key of its own or
    // dropping `unread_count` leaves `notifications-schema.ts` rejecting a
    // payload agent-api answered correctly -- and the bell is the one number
    // that envelope exists to carry.
    const payload = {
      notifications: [],
      unread_count: 4,
      notification_id: NOTIFICATION_ID,
    };
    fetchStub.mockResolvedValue(jsonResponse(payload, 200));

    const response = await call();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(payload);
  });
});

describe("the page route - its own query parameters ", () => {
  it("forwards unread_only and limit from its own request url", async () => {
    // Catches a proxy that drops the query -- the tool puts the unread
    // filter on the tool because the bell's own view is the unread set, so a
    // page that cannot ask for it makes that filter unreachable from the only
    // caller there is -- and one that renames either parameter on the way
    // through, which agent-api would ignore while answering a page nobody
    // asked for.
    await notificationsGET(pageRequest("?unread_only=true&limit=25"));

    const [url] = firstCall();
    const forwarded = new URL(String(url));
    expect(forwarded.origin + forwarded.pathname).toBe(`${UPSTREAM_ORIGIN}/me/notifications`);
    expect(forwarded.searchParams.get("unread_only")).toBe("true");
    expect(forwarded.searchParams.get("limit")).toBe("25");
    expect([...forwarded.searchParams.keys()]).toHaveLength(2);
  });

  it("sends neither parameter when its own url carries neither", async () => {
    // The first rule, seen from the browser. Catches a proxy that
    // always writes both keys: `?unread_only=&limit=` reaches agent-api as an
    // unparseable bool and an unparseable int, so a page that asked for
    // nothing would get a 422 instead of every message, where an absent pair
    // is what lets the tool's own defaults (`False` and 50) apply. Only the
    // presence test in the proxy can flip this row.
    await notificationsGET(pageRequest());

    const [url] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}/me/notifications`);
    expect(String(url)).not.toContain("?");
  });

  it("forwards only the parameter it was given", async () => {
    // The shape between the two above, which neither covers: an `unread_only`
    // asked for on its own must not drag an empty `limit` along with it.
    // Catches a proxy that reads both parameters and forwards both whatever it
    // found.
    await notificationsGET(pageRequest("?unread_only=true"));

    const [url] = firstCall();
    const forwarded = new URL(String(url));
    expect(forwarded.searchParams.get("unread_only")).toBe("true");
    expect(forwarded.searchParams.has("limit")).toBe(false);
  });

  it("forwards an empty unread_only or limit exactly as it was sent", async () => {
    // The far side decides. Catches a proxy that REWRITES an empty
    // value -- dropping it, or turning `unread_only=` into `false` -- which
    // would answer a page for a request agent-api was entitled to refuse and
    // hide a broken caller from whoever built it. The presence test is `!==
    // null`, not truthiness, and this row is the only one that can tell the
    // two apart.
    await notificationsGET(pageRequest("?unread_only=&limit="));

    const [url] = firstCall();
    const forwarded = new URL(String(url));
    expect(forwarded.searchParams.get("unread_only")).toBe("");
    expect(forwarded.searchParams.get("limit")).toBe("");
  });
});

describe("the stamp route - the wire", () => {
  it("posts to the notification's own path with no body at all", async () => {
    // Catches three proxies that
    // all look right on the read: one that forwards a GET (agent-api answers
    // 405 and nothing is ever stamped), one that invents a body -- a payload
    // this route does not validate and the tool does not read, which agent-api
    // would refuse as a 422 -- and one that ignores its own `params` and posts
    // to a fixed id, which would clear the wrong message from somebody's bell
    // while the one they clicked stayed unread.
    await readPOST(stampRequest(), {
      params: Promise.resolve({ notificationId: NOTIFICATION_ID }),
    });

    const [url, init] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}/notifications/${NOTIFICATION_ID}/read`);
    expect(init?.method).toBe("POST");
    expect(await sentBody(init)).toBe("");
  });

  it("sends no idempotency key of its own", async () => {
    // The third rule seen from the browser: the key is DERIVED by
    // agent-api from the verified reader and the notification. Catches a proxy
    // that mints one per click -- two keys for one double-clicked bell, which
    // is two calls filed on one row -- and one that forwards a key the browser
    // could choose, which is `IDEMPOTENCY_CONFLICT` taken by accident across
    // two different notifications.
    await readPOST(stampRequest(), {
      params: Promise.resolve({ notificationId: NOTIFICATION_ID }),
    });

    const [url, init] = firstCall();
    expect(await sentBody(init)).not.toContain("idempotency");
    expect(String(url)).not.toContain("idempotency");
    expect(new Headers(init?.headers).get("idempotency-key")).toBeNull();
  });
});

describe("the two notification routes - an unreachable agent-api", () => {
  it.each(routes)(
    "$name answers 502 UPSTREAM_UNAVAILABLE when the call throws",
    async ({ call }) => {
      // Catches a route whose `fetch` rejection is left to propagate: Next
      // answers an untyped 500 with a stack, which the page cannot map to any
      // sentence and which names the internal host. Only the transport branch
      // can produce this row -- the cookie is present and the token is never
      // rejected here.
      fetchStub.mockRejectedValue(new TypeError("fetch failed"));

      const response = await call();

      expect(response.status).toBe(UPSTREAM_UNAVAILABLE_STATUS);
      expect(await response.json()).toEqual({ code: "UPSTREAM_UNAVAILABLE" });
    },
  );
});

describe("the two notification routes - an expired session", () => {
  it.each(routes)(
    "$name answers a 307 to /login and expires the cookie on a 401",
    async ({ call }) => {
      // Catches a route that passes agent-api's 401 through to the browser: the
      // stale cookie survives, so every later call fails the same way with
      // nothing telling the user why. This is the `proxyAgentApi` seam, so a
      // route that opens its own `fetch` is caught here too -- an unrouted call
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
    // it already knows it will get -- a round trip, a log line and, on the
    // stamp route, a mutation attempt that cannot run.
    cookieValue = null;

    const response = await call();

    expect(fetchStub).toHaveBeenCalledTimes(0);
    expect(response.status).toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
  });
});
