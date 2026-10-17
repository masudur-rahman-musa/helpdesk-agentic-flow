// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { proxyAgentApi } from "@/lib/agent-api";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * A proxied call that answers 401 clears the cookie and redirects to /login.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked here — the module under test is the proxy, not Next's store.
 */

const TOKEN = "header.payload.signature";

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

beforeEach(() => {
  cookieValue = TOKEN;
  process.env.AGENT_API_URL = "http://agent-api.test:8000";
  fetchStub.mockReset();
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_API_URL;
});

describe("proxyAgentApi", () => {
  it("sends the bearer from the cookie and never in the URL", async () => {
    // Kills a proxy that puts the token in a query string, where it lands in
    // every access log and browser history entry.
    fetchStub.mockResolvedValue(new Response("{}", { status: 200 }));
    await proxyAgentApi("/me");
    const call = fetchStub.mock.calls[0];
    if (call === undefined) {
      throw new Error("expected agent-api to be called");
    }
    const [url, init] = call;
    expect(String(url)).not.toContain(TOKEN);
    expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
  });

  it("clears the cookie and redirects to /login on a 401", async () => {
    // Kills a proxy that passes the 401 through: the stale cookie survives, so
    // every later call fails the same way with nothing telling the user why.
    fetchStub.mockResolvedValue(
      new Response(JSON.stringify({ code: "FORBIDDEN" }), { status: 401 }),
    );
    const response = await proxyAgentApi("/me");
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
    const cookies = response.headers.getSetCookie();
    expect(cookies).toHaveLength(1);
    const [cookie] = cookies;
    expect(cookie?.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
    expect(/Max-Age=0|Expires=Thu, 01 Jan 1970/.test(cookie ?? "")).toBe(true);
  });

  it("redirects without calling agent-api when there is no cookie at all", async () => {
    // Kills a proxy that sends `Bearer null` upstream and waits for the 401 it
    // already knows it will get.
    cookieValue = null;
    const response = await proxyAgentApi("/me");
    expect(fetchStub).toHaveBeenCalledTimes(0);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
  });
});
