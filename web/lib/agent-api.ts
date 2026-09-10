/**
 * The only module that talks to agent-api. It runs on the server side alone,
 * so the browser never makes a cross-origin request and there is no CORS for
 * row 27 to open.
 *
 * Every call carries an explicit timeout: agent-api is an
 * external call that can hang, and a login waiting forever on it holds a Next
 * worker with nothing for the user to see. `AGENT_API_TIMEOUT_MS` is pinned at
 * its exact value, and the signal's presence on the outgoing call asserted, by
 * `tests/agent-api.test.ts`.
 *
 * `AGENT_API_URL` is read per call, not at module load, so a process that is
 * reconfigured — and a test that sets the variable per case — sees the change.
 */

import { NextResponse } from "next/server";

import type { LoginInput } from "@/lib/schemas";
import { clearSessionCookie, readSessionToken } from "@/lib/session";

/** Ten seconds: a login does one argon2 verification and three short queries. */
export const AGENT_API_TIMEOUT_MS = 10_000;

export type LoginResult = { ok: true; token: string } | { ok: false };

function baseUrl(): string {
  return process.env.AGENT_API_URL ?? "http://localhost:8000";
}

/**
 * Exchanges credentials for a bearer. A non-200, a body without a string
 * `access_token`, and a transport failure all answer the SAME `{ ok: false }`:
 * the caller has one refusal to render and agent-api's own body never reaches
 * it. Pinned by `tests/login-route.test.ts`'s "does not forward agent-api's
 * own error body".
 */
export async function requestToken(input: LoginInput): Promise<LoginResult> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}/auth/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(input),
      cache: "no-store",
      signal: AbortSignal.timeout(AGENT_API_TIMEOUT_MS),
    });
  } catch {
    return { ok: false };
  }

  if (!response.ok) {
    return { ok: false };
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { ok: false };
  }

  if (
    typeof body !== "object" ||
    body === null ||
    !("access_token" in body) ||
    typeof body.access_token !== "string" ||
    body.access_token.length === 0
  ) {
    return { ok: false };
  }

  return { ok: true, token: body.access_token };
}

/**
 * The shape every later proxied call takes:
 * attach the bearer from the cookie, and on a 401 from agent-api CLEAR that
 * cookie and send the browser to `/login`. A 401 means the token is expired,
 * revoked or forged, so keeping it would make every later call fail the same
 * way with nothing telling the user why.
 *
 * This plan adds no proxied call of its own — `/login` and `/logout` are the
 * only two route handlers, and neither carries a bearer — so there is no caller
 * yet. An earlier change's chat route is the first one, and routing it through here is
 * what keeps the expiry path in one place. Pinned by
 * `tests/agent-api-proxy.test.ts`; recorded in 22a's Deviations.
 */
export async function proxyAgentApi(path: string, init: RequestInit = {}): Promise<NextResponse> {
  const token = await readSessionToken();
  if (token === null) {
    return unauthenticated();
  }

  const headers = new Headers(init.headers);
  headers.set("authorization", `Bearer ${token}`);

  let upstream: Response;
  try {
    upstream = await fetch(`${baseUrl()}${path}`, {
      ...init,
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(AGENT_API_TIMEOUT_MS),
    });
  } catch {
    return NextResponse.json({ code: "UPSTREAM_UNAVAILABLE" }, { status: 502 });
  }

  if (upstream.status === 401) {
    return unauthenticated();
  }

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
  });
}

/** A cleared cookie and a redirect to the form, as one response. */
function unauthenticated(): NextResponse {
  const response = NextResponse.redirect(new URL("/login", baseOrigin()));
  clearSessionCookie(response);
  return response;
}

/**
 * The app's own origin. `NEXT_PUBLIC_APP_URL` when set, else localhost: a
 * redirect needs an absolute URL, and guessing it from the upstream base would
 * point the browser at agent-api.
 */
function baseOrigin(): string {
  return process.env.NEXT_PUBLIC_APP_URL ?? "http://localhost:3000";
}
