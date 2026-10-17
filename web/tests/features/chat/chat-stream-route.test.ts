// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/api/chat/stream/route";
import { AGENT_API_STREAM_TIMEOUT_MS } from "@/lib/agent-api";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * `chat-stream-route.test.ts`: `POST /api/chat/stream` against a
 * stubbed `fetch`. Node environment, because the handler is server-side and
 * `NextResponse.redirect` needs the real `Response`.
 *
 * `next/headers`' `cookies()` only exists inside a request scope, so it is
 * mocked exactly as `tests/agent-api-proxy.test.ts` mocks it — the module under
 * test is the route, not Next's store.
 *
 * Shapes this file fixes:
 * - the redirect status is **307**, read off `proxyAgentApi` rather than
 *   invented: it answers `NextResponse.redirect(url)`, whose default status is
 *   307, so the number is pinned here.
 * - the refusal envelope is `{ code: "VALIDATION" }`,
 *   agent-api's own shape from `chat_routes._typed`, and NOT the older
 *   `{ message }`.
 * - the timeout is asserted through a spy on `AbortSignal.timeout`, because a
 *   signal carries no readable duration: the case pins that the signal handed
 *   to `fetch` is the one built from `AGENT_API_STREAM_TIMEOUT_MS` and not the
 *   one `proxyAgentApi` builds from `AGENT_API_TIMEOUT_MS` (10s, which would
 *   abort an ordinary turn part-way through its own reply). An
 *   implementation that times out with `AbortController` + `setTimeout`
 *   instead would have to be reconciled with this case; `AbortSignal.timeout`
 *   is what every existing call in `lib/agent-api.ts` uses.
 * - the handler is `POST(request: Request): Promise<Response>`, the shape
 *   `app/api/login/route.ts` already has.
 */

const TOKEN = "header.payload.signature";
const STREAM_TIMEOUT_MS = 120_000;
const REDIRECT_STATUS = 307;
const CAP = 4000;

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

/** The signals `AbortSignal.timeout` handed out this test, by their duration. */
const signalsByMs = new Map<number, AbortSignal>();

function request(body: unknown): Request {
  return new Request("http://localhost:3000/api/chat/stream", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function sseResponse(payload = "event: done\ndata: {}\n\n"): Response {
  return new Response(payload, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function firstCall(): [RequestInfo | URL, RequestInit | undefined] {
  const call = fetchStub.mock.calls[0];
  if (call === undefined) {
    throw new Error("expected agent-api to be called");
  }
  return [call[0], call[1]];
}

beforeEach(() => {
  cookieValue = TOKEN;
  process.env.AGENT_API_URL = "http://agent-api.test:8000";
  signalsByMs.clear();
  vi.spyOn(AbortSignal, "timeout").mockImplementation((milliseconds: number) => {
    const controller = new AbortController();
    signalsByMs.set(milliseconds, controller.signal);
    return controller.signal;
  });
  fetchStub.mockReset();
  fetchStub.mockResolvedValue(sseResponse());
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete process.env.AGENT_API_URL;
});

describe("POST /api/chat/stream — the bearer", () => {
  it("sends the bearer from the httpOnly cookie and never in the URL", async () => {
    // Catches a route that reads a token from the request body or a header the
    // browser set — the cookie is the only copy of the bearer,
    // so anything else means the client was handed one. The URL half catches a
    // token put in a query string, where it lands in every access log.
    await POST(request({ message: "grant me the finance dashboard" }));

    const [url, init] = firstCall();
    expect(String(url)).toBe("http://agent-api.test:8000/chat/stream");
    expect(String(url)).not.toContain(TOKEN);
    expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
  });
});

describe("POST /api/chat/stream — the streaming timeout", () => {
  it("pins AGENT_API_STREAM_TIMEOUT_MS at exactly 120000", () => {
    // Catches a band ("some longer timeout"): the test wants the value,
    // and it is two minutes because `AbortSignal.timeout`
    // bounds the WHOLE response, body included.
    expect(AGENT_API_STREAM_TIMEOUT_MS).toBe(STREAM_TIMEOUT_MS);
  });

  it("puts the 120000ms signal — not the 10s login one — on the outgoing call", async () => {
    // Catches the route reusing `proxyAgentApi`'s hardcoded
    // `AbortSignal.timeout(AGENT_API_TIMEOUT_MS)`: the constant would exist,
    // read 120000, and never reach a stream, so every turn longer than ten
    // seconds would abort mid-reply and look exactly like a dropped socket. Identity against the
    // signal built from the stream constant is the
    // only thing that can tell the two apart — a signal carries no duration.
    await POST(request({ message: "grant me the finance dashboard" }));

    const [, init] = firstCall();
    const expected = signalsByMs.get(STREAM_TIMEOUT_MS);
    expect(expected).toBeInstanceOf(AbortSignal);
    expect(init?.signal).toBe(expected);
  });
});

describe("POST /api/chat/stream — an expired session", () => {
  it("answers a 307 to /login and expires the session cookie on a 401", async () => {
    // Catches a route that passes agent-api's 401 through to the browser: the
    // stale cookie survives, so every later call fails the same way with
    // nothing telling the user why, and the chat page renders a refusal it
    // cannot recover from. This is the `proxyAgentApi` seam and its first
    // real caller, so a route that opens its own `fetch` is caught here
    // too — an unrouted call answers 401, not a redirect.
    fetchStub.mockResolvedValue(
      new Response(JSON.stringify({ code: "FORBIDDEN" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      }),
    );

    const response = await POST(request({ message: "grant me the finance dashboard" }));

    expect(response.status).toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")?.endsWith("/login")).toBe(true);
    const cookies = response.headers.getSetCookie();
    expect(cookies).toHaveLength(1);
    const [cookie] = cookies;
    expect(cookie?.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
    expect(/Max-Age=0|Expires=Thu, 01 Jan 1970/.test(cookie ?? "")).toBe(true);
  });
});

describe("POST /api/chat/stream — the shared pre-check", () => {
  const rows = [
    { name: "the empty message", message: "", forwarded: false },
    { name: "one character", message: "x", forwarded: true },
    { name: "exactly the cap", message: "x".repeat(CAP), forwarded: true },
    { name: "one character over the cap", message: "x".repeat(CAP + 1), forwarded: false },
  ] as const;

  it.each(rows)("$name reaches agent-api: $forwarded", async ({ message, forwarded }) => {
    // The fetch stub is primed to SUCCEED, so the call COUNT is the only
    // assertion that can tell "checked before the call" from "checked after
    // it" — the same shape `tests/login-route.test.ts` uses. Catches a handler
    // that forwards the body and lets agent-api's own `check_message_content`
    // refuse it, which spends a round trip and a rate-limit slot on a message
    // the client already knew was unsendable, and catches an exclusive bound
    // that refuses a message of exactly 4000 characters agent-api accepts.
    const response = await POST(request({ message }));

    expect(fetchStub).toHaveBeenCalledTimes(forwarded ? 1 : 0);
    if (!forwarded) {
      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ code: "VALIDATION" });
    }
  });

  it("answers the typed code envelope and not 22a's message envelope", async () => {
    // Catches a refusal shaped `{ message: "..." }`: that key belongs to the
    // one human sentence `/login` renders. The chat client picks its
    // sentence from `FAILURE_MESSAGES` by CODE, so a body without `code` leaves
    // it rendering the default for a validation failure it could have named.
    const response = await POST(request({ message: "" }));

    const body: unknown = await response.json();
    expect(body).toEqual({ code: "VALIDATION" });
  });

  it("refuses an unparseable body without calling agent-api", async () => {
    // Catches a handler that lets `request.json()` throw: Next answers an
    // untyped 500, which the client cannot map to any sentence at all.
    const broken = new Request("http://localhost:3000/api/chat/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{not json",
    });

    const response = await POST(broken);

    expect(fetchStub).toHaveBeenCalledTimes(0);
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ code: "VALIDATION" });
  });
});

describe("POST /api/chat/stream — the stream itself", () => {
  it("passes the upstream content-type text/event-stream through", async () => {
    // Catches a route that answers `NextResponse.json(...)` or rewrites the
    // content type: a browser handed `application/json` for an SSE body has no
    // reason to read it incrementally, and every proxy in between is free to
    // buffer it.
    const response = await POST(request({ message: "grant me the finance dashboard" }));

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/event-stream");
  });

  it("delivers the first frame while the upstream stream is still open", async () => {
    // Catches a proxy that buffers — `await upstream.text()` and then one
    // `Response` — which turns a stream into one late blob while every test
    // that reads a SETTLED reply still passes (the main risk). The upstream
    // body is deliberately left OPEN: a buffering proxy has nothing to answer
    // with until it closes, so the race below resolves to the sentinel.
    const encoder = new TextEncoder();
    let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
    const upstream = new ReadableStream<Uint8Array>({
      start(streamController) {
        controller = streamController;
      },
    });
    fetchStub.mockResolvedValue(
      new Response(upstream, { status: 200, headers: { "content-type": "text/event-stream" } }),
    );

    const response = await POST(request({ message: "grant me the finance dashboard" }));
    controller?.enqueue(encoder.encode('event: token\ndata: {"text":"Hi"}\n\n'));

    const body = response.body;
    if (body === null) {
      throw new Error("expected the proxied response to carry a body");
    }
    const reader = body.getReader();
    const sentinel = Symbol("buffered");
    const first = await Promise.race([
      reader.read(),
      new Promise<typeof sentinel>((resolve) => setTimeout(() => resolve(sentinel), 500)),
    ]);
    controller?.close();

    expect(first).not.toBe(sentinel);
    if (first === sentinel) {
      throw new Error("the proxy buffered: no frame arrived while the upstream body was open");
    }
    expect(new TextDecoder().decode(first.value)).toBe('event: token\ndata: {"text":"Hi"}\n\n');
  });
});
