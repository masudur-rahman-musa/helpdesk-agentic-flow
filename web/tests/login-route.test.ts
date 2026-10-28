// @vitest-environment node
import { NextResponse } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST as loginPost } from "@/app/api/login/route";
import { POST as logoutPost } from "@/app/api/logout/route";
import { SIGN_IN_FAILED_MESSAGE } from "@/lib/schemas";
import { SESSION_COOKIE_NAME, clearSessionCookie, setSessionCookie } from "@/lib/session";

/**
 * The login route handler against a stubbed global `fetch`. No agent-api
 * process, no backing service. Every case names the wrong
 * handler it kills.
 *
 * Values fixed here:
 * the refusal body is `{ message: SIGN_IN_FAILED_MESSAGE }` — one key, named
 * `message` — and both schema refusals (a field failing the pre-check, and an
 * unparseable body) answer 400.
 */

const BEARER = "header.payload.signature-9f2c";
const AGENT_API_URL = "http://agent-api.test:8000";

/** The handler's own fetch, typed so a wrong call shape is a typecheck error. */
const fetchStub = vi.fn<typeof fetch>();

function loginRequest(body: unknown): Request {
  return new Request("http://localhost/api/login", {
    method: "POST",
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: { "content-type": "application/json" },
  });
}

function validBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    tenant_slug: "nordlicht",
    email: "ada@e.com",
    password: "correct-horse-battery-staple",
    ...overrides,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Narrowing helper: `noUncheckedIndexedAccess` makes `cookies[0]` optional. */
function onlyCookie(response: Response): string {
  const cookies = response.headers.getSetCookie();
  expect(cookies).toHaveLength(1);
  const [cookie] = cookies;
  if (cookie === undefined) {
    throw new Error("expected exactly one Set-Cookie header");
  }
  return cookie;
}

beforeEach(() => {
  process.env.AGENT_API_URL = AGENT_API_URL;
  fetchStub.mockReset();
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_API_URL;
});

describe("POST /api/login — agent-api answers 200", () => {
  beforeEach(() => {
    fetchStub.mockResolvedValue(jsonResponse(200, { access_token: BEARER, token_type: "bearer" }));
  });

  it("sets exactly one cookie, named helpdesk_session", async () => {
    // Kills a handler that sets a second, readable copy of the session for the
    // client (the "mirror it in a non-httpOnly cookie so the UI can read it"
    // shortcut): one correct cookie plus one convenience cookie is still two.
    const response = await loginPost(loginRequest(validBody()));
    const cookie = onlyCookie(response);
    expect(cookie.startsWith(`${SESSION_COOKIE_NAME}=`)).toBe(true);
  });

  it("serialises the session cookie with HttpOnly, SameSite=Lax and Path=/", async () => {
    // Kills the main risk directly: a cookie missing HttpOnly or SameSite
    // puts the bearer in reach of any script on the page. Asserted on the
    // serialised attribute strings, never on the cookie's value, because a
    // handler that sets the right value with the wrong flags is the defect.
    const response = await loginPost(loginRequest(validBody()));
    const cookie = onlyCookie(response);
    expect(cookie).toContain("HttpOnly");
    // Next 15 serialises the attribute lowercase (`SameSite=lax`); the value is
    // case-insensitive per RFC 6265bis, so the library's own casing is what is
    // pinned here rather than another spelling.
    expect(cookie).toContain("SameSite=lax");
    expect(cookie).toContain("Path=/");
  });

  it("never returns the bearer in the response body", async () => {
    // Kills a handler that also echoes the token to the client (the
    // server half). The raw text is searched, so no key name can hide it:
    // { token }, { data: { access_token } } and a bare string all turn red.
    const response = await loginPost(loginRequest(validBody()));
    const text = await response.text();
    expect(text).not.toContain(BEARER);
  });

  it("calls agent-api exactly once", async () => {
    // Kills a handler that retries a successful login (a duplicate argon2
    // verification per sign-in) or that probes agent-api before posting.
    await loginPost(loginRequest(validBody()));
    expect(fetchStub).toHaveBeenCalledTimes(1);
  });
});

describe("POST /api/login — agent-api answers 401", () => {
  beforeEach(() => {
    fetchStub.mockResolvedValue(jsonResponse(401, { code: "FORBIDDEN" }));
  });

  it("sets no cookie", async () => {
    // Kills a handler that writes the cookie before checking the upstream
    // status, which would leave a session cookie holding `undefined`.
    const response = await loginPost(loginRequest(validBody()));
    expect(response.headers.getSetCookie()).toEqual([]);
  });

  it("answers the one generic message and nothing field-level", async () => {
    // Kills a handler that hand-types its own refusal text, which drifts from
    // the form's message; the constant is the single source.
    const response = await loginPost(loginRequest(validBody()));
    const body: unknown = await response.json();
    expect(body).toEqual({ message: SIGN_IN_FAILED_MESSAGE });
  });

  it("does not forward agent-api's own error body", async () => {
    // Kills the main risk: a handler that proxies the upstream body turns one
    // generic refusal into an upstream error code the client can read and branch
    // on. agent-api's 401 body here is {"code":"FORBIDDEN"} and the word must
    // not survive into the response.
    const response = await loginPost(loginRequest(validBody()));
    const text = await response.text();
    expect(text).not.toContain("FORBIDDEN");
  });
});

describe("POST /api/login — the schema pre-check runs before any fetch", () => {
  const refused: ReadonlyArray<{
    label: string;
    body: Record<string, unknown>;
  }> = [
    {
      label: "an uppercase tenant slug",
      body: validBody({ tenant_slug: "Nordlicht" }),
    },
    {
      label: "a tenant slug of 65 characters",
      body: validBody({ tenant_slug: "a".repeat(65) }),
    },
    {
      label: "a tenant slug with a trailing newline",
      body: validBody({ tenant_slug: "nordlicht\n" }),
    },
    {
      label: "an email of 321 characters",
      body: validBody({ email: `${"a".repeat(315)}@e.com` }),
    },
  ];

  it.each(refused)("never calls fetch for $label", async ({ body }) => {
    // Kills a handler that validates AFTER the upstream call (or not at all):
    // the stub is primed to succeed, so only the call COUNT can tell the two
    // orders apart. This is the case that fails if the pre-check moves.
    fetchStub.mockResolvedValue(jsonResponse(200, { access_token: BEARER, token_type: "bearer" }));
    const response = await loginPost(loginRequest(body));
    expect(fetchStub).toHaveBeenCalledTimes(0);
    expect(response.status).toBe(400);
    expect(response.headers.getSetCookie()).toEqual([]);
  });

  it("never calls fetch for a malformed JSON body", async () => {
    // Kills a handler that awaits request.json() without handling the throw:
    // an unhandled parse error is a 500, and a handler that forwards the raw
    // text upstream instead would show a call count of 1.
    fetchStub.mockResolvedValue(jsonResponse(200, { access_token: BEARER, token_type: "bearer" }));
    const response = await loginPost(loginRequest("{not json"));
    expect(fetchStub).toHaveBeenCalledTimes(0);
    expect(response.status).toBe(400);
  });
});

describe("POST /api/logout", () => {
  it("clears the session cookie with an expiry in the past", async () => {
    // Kills a logout that only redirects, leaving the bearer cookie live in the
    // browser; and kills one that overwrites the value but forgets the expiry,
    // so the empty cookie survives the tab.
    const response = await logoutPost();
    const cookie = onlyCookie(response);
    expect(cookie.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
    expect(cookie).toContain("HttpOnly");
    expect(/Max-Age=0|Expires=Thu, 01 Jan 1970/.test(cookie)).toBe(true);
  });
});

describe("lib/session cookie helpers", () => {
  it("setSessionCookie writes the flags on the response it is given", () => {
    // Kills a helper that exists but sets a bare cookie while the route happens
    // to pass the flags itself: the route cases above cannot tell which of the
    // two wrote them, this one can.
    const response = NextResponse.json({ ok: true });
    setSessionCookie(response, BEARER);
    const cookie = onlyCookie(response);
    expect(cookie).toContain("HttpOnly");
    // Next 15 serialises the attribute lowercase (`SameSite=lax`); the value is
    // case-insensitive per RFC 6265bis, so the library's own casing is what is
    // pinned here rather than another spelling.
    expect(cookie).toContain("SameSite=lax");
    expect(cookie).toContain("Path=/");
  });

  it("clearSessionCookie expires the cookie on the response it is given", () => {
    // Kills a clear that sets a fresh empty cookie with a future lifetime, which
    // reads as a logout and behaves as a session that never ended.
    const response = NextResponse.json({ ok: true });
    clearSessionCookie(response);
    const cookie = onlyCookie(response);
    expect(cookie.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
    expect(/Max-Age=0|Expires=Thu, 01 Jan 1970/.test(cookie)).toBe(true);
  });
});
