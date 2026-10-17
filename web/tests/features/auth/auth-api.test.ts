import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { logIn, logOut } from "@/features/auth/api";
import { API } from "@/lib/api-paths";

/**
 * The auth data calls, over a stubbed `fetch`. No component, no network.
 */

const FIELDS = { tenant_slug: "acme", email: "a@example.test", password: "pw-123456" };

const fetchStub = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchStub.mockReset();
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("logIn", () => {
  it("posts the fields to API.login and answers true on 200", async () => {
    // Kill: catches a call that hits another path, drops a field, skips the JSON
    // body, or answers false on success.
    fetchStub.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await expect(logIn(FIELDS)).resolves.toBe(true);
    expect(fetchStub).toHaveBeenCalledTimes(1);
    const [url, init] = fetchStub.mock.calls[0] ?? [];
    expect(url).toBe(API.login);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual(FIELDS);
  });

  it("answers false on a 401", async () => {
    // Kill: catches a call that throws on a refusal, or treats any answer as success.
    fetchStub.mockResolvedValue(new Response("{}", { status: 401 }));
    await expect(logIn(FIELDS)).resolves.toBe(false);
  });

  it("answers false when fetch throws", async () => {
    // Kill: catches a call that lets a dropped network escape as an exception
    // instead of the false the form shows as a failed sign-in.
    fetchStub.mockRejectedValue(new TypeError("network down"));
    await expect(logIn(FIELDS)).resolves.toBe(false);
  });
});

describe("logOut", () => {
  it("posts to API.logout", async () => {
    // Kill: catches a call that uses GET, sends a body, or hits another path.
    fetchStub.mockResolvedValue(new Response(null, { status: 200 }));
    await logOut();
    expect(fetchStub).toHaveBeenCalledTimes(1);
    const [url, init] = fetchStub.mock.calls[0] ?? [];
    expect(url).toBe(API.logout);
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeUndefined();
  });
});
