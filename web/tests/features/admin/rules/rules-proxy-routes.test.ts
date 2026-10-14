// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as rulesGET } from "@/app/api/admin/rules/route";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * An earlier change §6, `rules-proxy-routes.test.ts`: the SoD rules board, proxied,
 * against a stubbed `fetch`. Node environment, because the handler is
 * server-side and `NextResponse.redirect` needs the real `Response`.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked exactly as `tests/killswitch-proxy-routes.test.ts` mocks it — the
 * module under test is the route, not Next's store.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 24eb's Deviations):
 * - the handler is `export async function GET(): Promise<Response>`, 23b's
 *   zero-argument shape, because the agent-api route takes no parameter at all.
 * - the upstream path is agent-api's own `/admin/rules` (decision C), never the
 *   browser-facing `/api/...` prefix, with no query string and no body.
 * - the transport-failure envelope is `502 { code: "UPSTREAM_UNAVAILABLE" }`,
 *   `proxyAgentApi`'s own body, and the 401 answer is a **307** to `/login`
 *   with the session cookie expired, read off `NextResponse.redirect`'s default
 *   status the way `tests/killswitch-proxy-routes.test.ts` reads it. The plan's
 *   §6 says only "clears the session cookie"; the seam does both in one
 *   response, so both are asserted here.
 *
 * No backing service and no page: the only seam is `fetch`.
 */

const TOKEN = "header.payload.signature";
const UPSTREAM_ORIGIN = "http://agent-api.test:8000";
const UPSTREAM_PATH = "/admin/rules";
const REDIRECT_STATUS = 307;
const UPSTREAM_UNAVAILABLE_STATUS = 502;
const FORBIDDEN_STATUS = 403;
const UNAVAILABLE_STATUS = 503;

/** One board as `list_sod_rules` answers it, obviously fake, one resolved row. */
const BOARD_BODY = {
  rules: [
    {
      rule_id: "cccccccc-cccc-5ccc-8ccc-cccccccccccc",
      role_a_id: "55555555-5555-5555-8555-555555555555",
      role_a: {
        role_id: "55555555-5555-5555-8555-555555555555",
        slug: "fake-payments-approver",
        name: "Fake Payments Approver",
        system_slug: "fake-finance-app",
        system_name: "Obviously Fake Finance App",
      },
      role_b_id: "66666666-6666-5666-8666-666666666666",
      role_b: null,
      rationale: "Obviously fake rationale, tests only.",
      policy_ref: "FAKE-SOD-001",
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

describe("the rules proxy — the bearer, the path and the empty request", () => {
  it("sends the bearer from the httpOnly cookie and never in the URL", async () => {
    // Catches a route that reads a token from a header the browser set or from
    // the query string — the cookie is the only copy of the bearer (22a
    // decision A), so anything else means the client was handed one, and a
    // token in a URL lands in every access log. Also catches a route that
    // calls agent-api directly instead of through `proxyAgentApi`: an unrouted
    // call carries no authorization header at all, which on this resource means
    // an admin read answered for nobody.
    await rulesGET();

    const [url, init] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}${UPSTREAM_PATH}`);
    expect(String(url)).not.toContain(TOKEN);
    expect(init?.method).toBe("GET");
    expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
  });

  it("sends no query string and no body at all", async () => {
    // §1's last criterion, as the wire sees it: this plan introduces NO input
    // that shapes a key, a path or a stored value. Catches a proxy that
    // forwarded the browser's search params (a `tenant_id` or a `rule_id`
    // riding along would cross back as a `VALIDATION`, because
    // `ListSodRulesInput` forbids extras) and one that reused a write verb's
    // `init`, which puts a body on a read path.
    await rulesGET();

    const [url, init] = firstCall();
    expect(String(url)).not.toContain("?");
    expect(await sentBody(init)).toBe("");
  });

  it("answers the upstream body unchanged, envelope included", async () => {
    // Catches a route that reshapes or re-wraps what agent-api sent: the page
    // runs one `safeParse` on the tool's own `{ rules: [...] }` envelope
    // (decision D), so a proxy that unwrapped it to a bare list, or added a key
    // of its own, leaves `rules-schema.ts` refusing a payload agent-api
    // answered correctly. A proxy that dropped the rows whose `role_b` is null
    // would also hide exactly the rules an admin came to fix (§5's first two
    // risks).
    const response = await rulesGET();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(BOARD_BODY);
  });
});

describe("the rules proxy — the refusals", () => {
  it.each([FORBIDDEN_STATUS, UNAVAILABLE_STATUS])(
    "passes a %i through with its own typed body",
    async (status) => {
      // §6's last web row. A 403 is `FORBIDDEN` (one of the five principals
      // with no cell on this row) and a 503 is `TOOL_DISABLED` (a switch
      // tripped on `list_sod_rules`). Catches a proxy that treats every non-200
      // as a transport failure: the admin would be told the platform is
      // unreachable — which reads as an outage — rather than that the platform
      // refused or is paused.
      //
      // Only the non-200 handling can decide these rows: the cookie is present,
      // the fetch resolves, and 401 is the sole status `proxyAgentApi` acts on.
      fetchStub.mockResolvedValue(
        jsonResponse({ code: status === FORBIDDEN_STATUS ? "FORBIDDEN" : "TOOL_DISABLED" }, status),
      );

      const response = await rulesGET();

      expect(response.status).toBe(status);
      expect(await response.json()).toEqual({
        code: status === FORBIDDEN_STATUS ? "FORBIDDEN" : "TOOL_DISABLED",
      });
    },
  );

  it("answers 502 UPSTREAM_UNAVAILABLE when the call throws", async () => {
    // Catches a route whose `fetch` rejection is left to propagate: Next
    // answers an untyped 500 with a stack, which the page cannot map to any
    // sentence and which names the internal host. Only the transport branch can
    // produce this row — the cookie is present and no status is returned at all.
    fetchStub.mockRejectedValue(new TypeError("fetch failed"));

    const response = await rulesGET();

    expect(response.status).toBe(UPSTREAM_UNAVAILABLE_STATUS);
    expect(await response.json()).toEqual({ code: "UPSTREAM_UNAVAILABLE" });
  });

  it("expires the session cookie and redirects to /login on a 401", async () => {
    // §6's third web row. Catches a route that passes agent-api's 401 through
    // to the browser: the stale cookie survives, so every later call fails the
    // same way with nothing telling the admin why. This is 22a's `proxyAgentApi`
    // seam, so a route that opens its own `fetch` is caught here too — an
    // unrouted call answers 401, not a redirect, and clears no cookie.
    fetchStub.mockResolvedValue(jsonResponse({ code: "FORBIDDEN" }, 401));

    const response = await rulesGET();

    expect(response.status).toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
    const cookies = response.headers.getSetCookie();
    expect(cookies).toHaveLength(1);
    const [cookie] = cookies;
    expect(cookie?.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
    expect(/Max-Age=0|Expires=Thu, 01 Jan 1970/.test(cookie ?? "")).toBe(true);
  });
});
