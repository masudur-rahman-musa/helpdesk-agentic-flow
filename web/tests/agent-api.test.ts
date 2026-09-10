// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AGENT_API_TIMEOUT_MS, requestToken } from "@/lib/agent-api";

/**
 * a project rule: agent-api is an external call that can hang, so it carries
 * an explicit timeout and the value is pinned exactly. An earlier change's §6 named no
 * case for it (recorded in the plan's Deviations); these are that case.
 */

const VALID_INPUT = {
  tenant_slug: "nordlicht",
  email: "ada@e.com",
  password: "correct-horse-battery-staple",
} as const;

const fetchStub = vi.fn<typeof fetch>();

beforeEach(() => {
  process.env.AGENT_API_URL = "http://agent-api.test:8000";
  fetchStub.mockReset();
  fetchStub.mockResolvedValue(
    new Response(JSON.stringify({ access_token: "a.b.c", token_type: "bearer" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_API_URL;
});

describe("agent-api timeout", () => {
  it("pins the timeout at exactly 10000ms", () => {
    // Kills a band ("some timeout is set"): a project rule wants the value,
    // and a 10-minute timeout is indistinguishable from none to a waiting user.
    expect(AGENT_API_TIMEOUT_MS).toBe(10_000);
  });

  it("carries an abort signal on the outgoing login call", async () => {
    // Kills a constant that exists and is never passed to fetch, which is how
    // a timeout gets "added" without taking effect.
    await requestToken(VALID_INPUT);
    const call = fetchStub.mock.calls[0];
    if (call === undefined) {
      throw new Error("expected agent-api to be called once");
    }
    const init = call[1];
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("answers a refusal when the transport throws", async () => {
    // Kills a caller that lets an aborted or refused connection propagate as a
    // 500: a timed-out login must read to the user as the same generic refusal.
    fetchStub.mockRejectedValue(new Error("timed out"));
    expect(await requestToken(VALID_INPUT)).toEqual({ ok: false });
  });

  it("answers a refusal when a 200 body carries no access token", async () => {
    // Kills a caller that trusts the upstream shape and writes `undefined` into
    // the session cookie, which reads as a signed-in user holding no bearer.
    fetchStub.mockResolvedValue(
      new Response(JSON.stringify({ token_type: "bearer" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    expect(await requestToken(VALID_INPUT)).toEqual({ ok: false });
  });
});
