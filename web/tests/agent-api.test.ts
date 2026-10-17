// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AGENT_API_REINGEST_TIMEOUT_MS, AGENT_API_TIMEOUT_MS, requestToken } from "@/lib/agent-api";

/**
 * agent-api is an external call that can hang, so it carries an explicit
 * timeout and the value is pinned exactly.
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
    // Kills a band ("some timeout is set"): the exact value matters,
    // and a 10-minute timeout is indistinguishable from none to a waiting user.
    expect(AGENT_API_TIMEOUT_MS).toBe(10_000);
  });

  it("pins the reingest timeout at exactly 35000ms and above agent-api's MCP read timeout", () => {
    // Kills
    // the re-ingest proxy left on `AGENT_API_TIMEOUT_MS`: a re-ingest is a
    // gateway redaction call plus an embed pass, and the process's first
    // `FastEmbedEmbedder` load can outrun ten seconds while the ingest itself
    // completes, so the admin reads an abort as a failure over a corpus that
    // DID change.
    //
    // The second assertion is why the number is what it is, and it is a
    // different kill from the first: agent-api's own `MCP_READ_TIMEOUT_SECONDS`
    // is 30.0, so anything at or below 30_000 makes the PROXY abort first and
    // the tool's typed refusal — `FORBIDDEN`, `NOT_FOUND`, `TOOL_DISABLED` —
    // never reaches the browser, which then shows "try again in a moment" for a
    // permission the admin does not have. A bound written in seconds (`35`)
    // fails both halves at once.
    expect(AGENT_API_REINGEST_TIMEOUT_MS).toBe(35_000);
    expect(AGENT_API_REINGEST_TIMEOUT_MS).toBeGreaterThan(30_000);
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
