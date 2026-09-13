// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as policiesGET } from "@/app/api/admin/policies/route";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * An earlier change: the policy documents board, proxied, against a stubbed `fetch`.
 *
 * NOT named by the plan's §6, which lists only `policies-schema.test.ts` for
 * the web half while §1 carries a criterion for the proxy itself
 * ("`web/app/api/admin/policies/route.ts` proxies with no query string and no
 * body"). A criterion with no case cannot be ticked by anyone (standing rule
 * 56), so this file is the missing home and belongs in 24g's Deviations.
 *
 * Node environment, because the handler is server-side and
 * `NextResponse.redirect` needs the real `Response`. `next/headers`' `cookies()`
 * only exists inside a request scope, so it is mocked exactly as
 * `tests/rules-proxy-routes.test.ts` mocks it — the module under test is the
 * route, not Next's store.
 *
 * Shapes this file fixes that the plan left open:
 * - the handler is `export async function GET(): Promise<Response>`, 23b's
 *   zero-argument shape, because the agent-api route takes no parameter at all.
 * - the upstream path is agent-api's own `/admin/policies`, never the
 *   browser-facing `/api/...` prefix.
 * - the transport-failure envelope is `502 { code: "UPSTREAM_UNAVAILABLE" }`,
 *   `proxyAgentApi`'s own body, and the 401 answer is a **307** to `/login`
 *   with the session cookie expired, read off `NextResponse.redirect`'s
 *   default status the way `tests/rules-proxy-routes.test.ts` reads it.
 *
 * No backing service and no page: the only seam is `fetch`.
 */

const TOKEN = "header.payload.signature";
const UPSTREAM_ORIGIN = "http://agent-api.test:8000";
const UPSTREAM_PATH = "/admin/policies";
const REDIRECT_STATUS = 307;
const UPSTREAM_UNAVAILABLE_STATUS = 502;
const FORBIDDEN_STATUS = 403;
const UNAVAILABLE_STATUS = 503;

/** One board as `list_policy_documents` answers it, obviously fake. */
const BOARD_BODY = {
  documents: [
    {
      slug: "fake-access-policy",
      title: "Fake Access Standard",
      versions: [
        {
          version: "v9",
          is_current: true,
          ingested_at: "2026-04-01T09:00:00Z",
          chunk_count: 3,
          source_path: "policies/fake-tenant/v9/fake-access-policy.md",
          sha256: "a9".repeat(32),
        },
        {
          version: "v10",
          is_current: false,
          ingested_at: "2026-03-01T09:00:00Z",
          chunk_count: 0,
          source_path: "policies/fake-tenant/v10/fake-access-policy.md",
          sha256: "b0".repeat(32),
        },
      ],
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

describe("the policies proxy — the bearer, the path and the empty request", () => {
  it("sends the bearer from the httpOnly cookie and never in the URL", async () => {
    // Catches a route that reads a token from a header the browser set or from
    // the query string — the cookie is the only copy of the bearer (22a
    // decision A), so anything else means the client was handed one, and a
    // token in a URL lands in every access log. Also catches a route that
    // calls agent-api directly instead of through `proxyAgentApi`: an unrouted
    // call carries no authorization header at all, which on this resource
    // means an admin read answered for nobody.
    await policiesGET();

    const [url, init] = firstCall();
    expect(String(url)).toBe(`${UPSTREAM_ORIGIN}${UPSTREAM_PATH}`);
    expect(String(url)).not.toContain(TOKEN);
    expect(init?.method).toBe("GET");
    expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
  });

  it("sends no query string and no body at all", async () => {
    // §1's last criterion, as the wire sees it: this plan introduces NO input
    // of any kind. Catches a proxy that forwarded the browser's search params
    // (a `slug` or a `tenant_id` riding along would cross back as a
    // `VALIDATION`, because `ListPolicyDocumentsInput` forbids extras) and one
    // that reused a write verb's `init`, which puts a body on a read path.
    await policiesGET();

    const [url, init] = firstCall();
    expect(String(url)).not.toContain("?");
    expect(await sentBody(init)).toBe("");
  });

  it("answers the upstream body unchanged, envelope included", async () => {
    // Catches a route that reshapes or re-wraps what agent-api sent: the page
    // runs one `safeParse` on the tool's own `{ documents: [...] }` envelope,
    // so a proxy that unwrapped it to a bare list, flattened the versions into
    // one row each, or added a key of its own leaves `policies-schema.ts`
    // refusing a payload agent-api answered correctly. A proxy that dropped
    // the version whose `chunk_count` is 0 would also hide exactly the ingest
    // failure this page exists to surface.
    const response = await policiesGET();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(BOARD_BODY);
  });
});

describe("the policies proxy — the refusals", () => {
  it.each([FORBIDDEN_STATUS, UNAVAILABLE_STATUS])(
    "passes a %i through with its own typed body",
    async (status) => {
      // A 403 is `FORBIDDEN` (one of the five principals with no cell on this
      // row) and a 503 is `TOOL_DISABLED` (a switch tripped on
      // `list_policy_documents`). Catches a proxy that treats every non-200 as
      // a transport failure: the admin would be told the platform is
      // unreachable — which reads as an outage — rather than that the platform
      // refused or is paused.
      //
      // Only the non-200 handling can decide these rows: the cookie is
      // present, the fetch resolves, and 401 is the sole status
      // `proxyAgentApi` acts on.
      fetchStub.mockResolvedValue(
        jsonResponse({ code: status === FORBIDDEN_STATUS ? "FORBIDDEN" : "TOOL_DISABLED" }, status),
      );

      const response = await policiesGET();

      expect(response.status).toBe(status);
      expect(await response.json()).toEqual({
        code: status === FORBIDDEN_STATUS ? "FORBIDDEN" : "TOOL_DISABLED",
      });
    },
  );

  it("answers 502 UPSTREAM_UNAVAILABLE when the call throws", async () => {
    // Catches a route whose `fetch` rejection is left to propagate: Next
    // answers an untyped 500 with a stack, which the page cannot map to any
    // sentence and which names the internal host. Only the transport branch
    // can produce this row — the cookie is present and no status is returned
    // at all.
    fetchStub.mockRejectedValue(new TypeError("fetch failed"));

    const response = await policiesGET();

    expect(response.status).toBe(UPSTREAM_UNAVAILABLE_STATUS);
    expect(await response.json()).toEqual({ code: "UPSTREAM_UNAVAILABLE" });
  });

  it("expires the session cookie and redirects to /login on a 401", async () => {
    // Catches a route that passes agent-api's 401 through to the browser: the
    // stale cookie survives, so every later call fails the same way with
    // nothing telling the admin why, and an empty corpus page would read as
    // "this tenant has no policies" instead of "your session ended". This is
    // 22a's `proxyAgentApi` seam, so a route that opens its own `fetch` is
    // caught here too — an unrouted call answers 401, not a redirect, and
    // clears no cookie.
    fetchStub.mockResolvedValue(jsonResponse({ code: "FORBIDDEN" }, 401));

    const response = await policiesGET();

    expect(response.status).toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
    const cookies = response.headers.getSetCookie();
    expect(cookies).toHaveLength(1);
    const [cookie] = cookies;
    expect(cookie?.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
    expect(/Max-Age=0|Expires=Thu, 01 Jan 1970/.test(cookie ?? "")).toBe(true);
  });
});
