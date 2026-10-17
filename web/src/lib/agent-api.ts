/**
 * The only module that talks to agent-api. It runs on the server alone, so the
 * browser makes no cross-origin calls. Every call has a timeout, so a hung
 * agent-api never holds a worker forever. `AGENT_API_URL` is read per call so a
 * changed setting takes effect.
 */

import { NextResponse } from "next/server";

import type { LoginInput } from "@/lib/schemas";
import { clearSessionCookie, readSessionToken } from "@/lib/session";

/** Ten seconds: a login does one argon2 verification and three short queries. */
export const AGENT_API_TIMEOUT_MS = 10_000;

/**
 * Two minutes, for a streamed turn. The timeout covers the whole body, so ten
 * seconds would cut an ordinary reply short.
 */
export const AGENT_API_STREAM_TIMEOUT_MS = 120_000;

/**
 * Thirty-five seconds, for one policy re-ingest, which can take longer than ten.
 * It sits above agent-api's own 30 second tool timeout, so the typed refusal
 * arrives before this call gives up.
 */
export const AGENT_API_REINGEST_TIMEOUT_MS = 35_000;

export type LoginResult = { ok: true; token: string } | { ok: false };

function baseUrl(): string {
  return process.env.AGENT_API_URL ?? "http://localhost:8000";
}

/**
 * Exchanges credentials for a bearer. Every failure gives the same
 * `{ ok: false }`, so agent-api's own error body never reaches the caller.
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
 * Proxy one call with the bearer from the cookie. A 401 clears the cookie and
 * sends the browser to `/login`, since a bad token would only fail again. The
 * signal is set after `...init`, so a caller cannot make an untimed call.
 */
export async function proxyAgentApi(
  path: string,
  init: RequestInit = {},
  timeoutMs: number = AGENT_API_TIMEOUT_MS,
): Promise<NextResponse> {
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
      signal: AbortSignal.timeout(timeoutMs),
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
