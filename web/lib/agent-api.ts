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

/**
 * Two minutes, for a streamed turn alone.
 *
 * `AbortSignal.timeout` bounds the WHOLE response, its body included, so the
 * ten seconds above would abort an ordinary turn part-way through its own
 * reply and look exactly like a dropped socket. a project rule still holds —
 * the stream carries a timeout, it is just a different number — and the value
 * is pinned, and its signal traced onto the outgoing call, by
 * `tests/chat-stream-route.test.ts`.
 */
export const AGENT_API_STREAM_TIMEOUT_MS = 120_000;

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
 * An earlier change's chat routes are the first callers, and routing them through here
 * is what keeps the expiry path in one place. Pinned by
 * `tests/agent-api-proxy.test.ts` and, for the streamed caller, by
 * `tests/chat-stream-route.test.ts`'s 401 case.
 *
 * `timeoutMs` is a PARAMETER rather than the constant this function used to
 * hardcode: the signal is set after the `...init` spread — deliberately, so a
 * caller cannot proxy an untimed call — which left a streaming caller unable
 * to ask for anything but ten seconds. The default is
 * unchanged, so every existing caller is bounded exactly as before.
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
