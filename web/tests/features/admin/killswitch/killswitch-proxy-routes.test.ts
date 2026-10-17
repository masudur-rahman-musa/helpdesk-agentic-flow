// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as killswitchGET, POST as killswitchPOST } from "@/app/api/admin/killswitch/route";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * `killswitch-proxy-routes.test.ts`: the kill switch board and
 * the toggle, proxied, against a stubbed `fetch`. Node environment, because
 * both handlers are server-side and `NextResponse.redirect` needs the real
 * `Response`.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked exactly as `tests/audit-proxy-routes.test.ts` mocks it — the module
 * under test is the route, not Next's store.
 *
 * Shapes this file fixes:
 * - the board handler is `export async function GET(): Promise<Response>`,
 *   the zero-argument shape, because the agent-api route takes no parameter
 *   at all; the toggle is `export async function POST(request: Request):
 *   Promise<Response>`, the shape the approval proxies use minus the path
 *   segment (both verbs share one path).
 * - the upstream path is agent-api's own `/admin/killswitch` for both verbs,
 *   never the browser-facing `/api/...` prefix, and with no query at all.
 * - the toggle's body is forwarded VERBATIM as the bytes that arrived, with
 *   `content-type: application/json`: the `tool_name` and `reason` bounds are
 *   the tool's, so a proxy that re-parsed and re-serialised would be a second
 *   opinion about a payload it does not validate.
 * - the transport-failure envelope is `502 { code: "UPSTREAM_UNAVAILABLE" }`,
 *   `proxyAgentApi`'s own body, and the 401 answer is a **307** to `/login`,
 *   read off `NextResponse.redirect`'s default status the same way
 *   `tests/audit-proxy-routes.test.ts` reads it (the
 *   redirect is observed on the response's status and `location` header, never
 *   through `window.location`).
 *
 * No backing service and no page: the only seam is `fetch`.
 */

const TOKEN = "header.payload.signature";
const UPSTREAM_ORIGIN = "http://agent-api.test:8000";
const UPSTREAM_PATH = "/admin/killswitch";
const REDIRECT_STATUS = 307;
const UPSTREAM_UNAVAILABLE_STATUS = 502;
const CONFLICT_STATUS = 409;
const UNAVAILABLE_STATUS = 503;

/** The browser's half of a toggle: the exact four keys, obviously fake. */
const TOGGLE_BODY = {
  tool_name: "grant_access",
  enabled: true,
  scope: "global",
  reason: "Obviously fake reason, tests only.",
};

const BOARD_BODY = { switches: [] };

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

function toggleRequest(body: unknown = TOGGLE_BODY): Request {
  return new Request(`http://web.test/api/admin/killswitch`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

/**
 * The two verbs as one table, each reduced to a no-argument call. The refusal
 * cases run over both, because a guard present on the board and missing on the
 * toggle is exactly the hole a per-verb case cannot see — and the toggle is
 * the write.
 */
const verbs = [
  { name: "GET /api/admin/killswitch", method: "GET", call: () => killswitchGET() },
  {
    name: "POST /api/admin/killswitch",
    method: "POST",
    call: () => killswitchPOST(toggleRequest()),
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
  fetchStub.mockResolvedValue(jsonResponse(BOARD_BODY, 200));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete process.env.AGENT_API_URL;
});

describe("the kill switch proxy — the bearer and the path", () => {
  it.each(verbs)(
    "$name sends the bearer from the httpOnly cookie and never in the URL",
    async ({ call, method }) => {
      // Catches a route that reads a token from a header the browser set or
      // from the query string — the cookie is the only copy of the bearer, so anything else means
      // the client was handed one, and a
      // token in a URL lands in every access log. Also catches a route that
      // calls agent-api directly instead of through `proxyAgentApi`: an
      // unrouted call carries no authorization header at all, and on the write
      // verb that is a toggle reaching the platform's kill switch with no
      // principal attached.
      await call();

      const [url, init] = firstCall();
      expect(String(url)).toBe(`${UPSTREAM_ORIGIN}${UPSTREAM_PATH}`);
      expect(String(url)).not.toContain(TOKEN);
      expect(String(url)).not.toContain("?");
      expect(init?.method).toBe(method);
      expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
    },
  );

  it("answers the board body unchanged", async () => {
    // Catches a route that reshapes or re-wraps what agent-api sent: the page
    // parses the tool's own envelope, so a proxy adding a key of its own leaves
    // `killswitch-schema.ts` rejecting a payload agent-api answered correctly.
    // A proxy that filtered rows would also be a second, invisible view of
    // which tools are stopped, over the one the tool already answered.
    const payload = {
      switches: [
        {
          tool: "grant_access",
          protected: false,
          stopped: false,
          global_state: null,
          tenant_state: null,
          unreadable: false,
        },
      ],
    };
    fetchStub.mockResolvedValue(jsonResponse(payload, 200));

    const response = await killswitchGET();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(payload);
  });

  it("sends no body at all on the board read", async () => {
    // Catches a proxy that reuses the toggle's `init` for both verbs: a GET
    // carrying a body is refused outright by some runtimes and, where it is
    // not, it is a write-shaped request on a read path. The board takes no
    // argument of any kind (the tenant comes from the token).
    await killswitchGET();

    const [, init] = firstCall();
    expect(await sentBody(init)).toBe("");
  });
});

describe("the kill switch proxy — the toggle's body", () => {
  it("forwards the body verbatim with its content type", async () => {
    // Catches a proxy that re-parses and re-serialises
    // the payload — a second opinion about a body it does not validate, which
    // would drop the one key it had not heard of (the `scope` key)
    // and silently turn a tenant-scoped stop into whatever its default is —
    // and one that sends no content type, which leaves FastAPI unable to read
    // the body at all and answers a refusal for a perfectly good toggle.
    await killswitchPOST(toggleRequest());

    const [url, init] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}${UPSTREAM_PATH}`);
    expect(await sentBody(init)).toBe(JSON.stringify(TOGGLE_BODY));
    expect(new Headers(init?.headers).get("content-type")).toBe("application/json");
  });

  it("forwards a body it cannot itself validate, byte for byte", async () => {
    // The other half of "verbatim": an over-long `reason` and an out-of-enum
    // `scope` are agent-api's to refuse with the typed `VALIDATION` the page has a
    // sentence for. Catches a proxy that validates the payload here and
    // answers its own refusal, which would be a second bound to keep in step
    // with `helpdesk.killswitch`'s own constants and a body shape the page
    // cannot map to any typed error code.
    const rejected = { tool_name: "", enabled: true, scope: "everything", reason: "" };

    await killswitchPOST(toggleRequest(rejected));

    const [, init] = firstCall();
    expect(await sentBody(init)).toBe(JSON.stringify(rejected));
  });

  it.each([CONFLICT_STATUS, UNAVAILABLE_STATUS])(
    "passes a %i through with its own typed body",
    async (status) => {
      // A 409 is `POLICY_BLOCKED` (an admin aiming the
      // switch at `set_kill_switch` itself) and a 503 is `TOOL_DISABLED` (the
      // switch already tripped on the tool being read). Catches a proxy that
      // treats every non-200 as a transport failure: the admin would be told
      // the platform is unreachable — which reads as an outage — rather than
      // that the platform refused, on the one control somebody would reach for
      // DURING an outage.
      //
      // Only the non-200 handling can decide these rows: the cookie is
      // present, the fetch resolves, and 401 is the sole status
      // `proxyAgentApi` acts on.
      fetchStub.mockResolvedValue(
        jsonResponse(
          { code: status === CONFLICT_STATUS ? "POLICY_BLOCKED" : "TOOL_DISABLED" },
          status,
        ),
      );

      const response = await killswitchPOST(toggleRequest());

      expect(response.status).toBe(status);
      expect(await response.json()).toEqual({
        code: status === CONFLICT_STATUS ? "POLICY_BLOCKED" : "TOOL_DISABLED",
      });
    },
  );
});

describe("the kill switch proxy — an unreachable agent-api", () => {
  it.each(verbs)(
    "$name answers 502 UPSTREAM_UNAVAILABLE when the call throws",
    async ({ call }) => {
      // Catches a route whose `fetch` rejection is left to propagate: Next
      // answers an untyped 500 with a stack, which the page cannot map to any
      // sentence and which names the internal host. Only the transport branch can
      // produce this row — the cookie is present and the token is never rejected
      // here.
      fetchStub.mockRejectedValue(new TypeError("fetch failed"));

      const response = await call();

      expect(response.status).toBe(UPSTREAM_UNAVAILABLE_STATUS);
      expect(await response.json()).toEqual({ code: "UPSTREAM_UNAVAILABLE" });
    },
  );
});

describe("the kill switch proxy — an expired session", () => {
  it.each(verbs)(
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

  it.each(verbs)("$name redirects without calling agent-api with no cookie", async ({ call }) => {
    // Catches a route that sends `Bearer null` upstream and waits for the 401
    // it already knows it will get — a round trip and a log line per load, and
    // on the write verb a mutation attempted with no identity at all.
    cookieValue = null;

    const response = await call();

    expect(fetchStub).toHaveBeenCalledTimes(0);
    expect(response.status).toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
  });
});
