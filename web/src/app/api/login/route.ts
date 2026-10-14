/**
 * POST /api/login — the cookie proxy.
 *
 * Order is the whole point: parse, then pre-check against `loginSchema`, and
 * only then call agent-api. `tests/login-route.test.ts` primes the fetch stub
 * to SUCCEED and asserts the call count is 0 for a refused body, which is the
 * only assertion that can tell this order from the other one.
 *
 * Every refusal — unparseable body, failed pre-check, upstream 401 — answers
 * the same `SIGN_IN_FAILED_MESSAGE` and sets no cookie, so nothing here says
 * which of the three inputs was wrong.
 */

import { NextResponse } from "next/server";

import { requestToken } from "@/lib/agent-api";
import { SIGN_IN_FAILED_MESSAGE, loginSchema } from "@/lib/schemas";
import { setSessionCookie } from "@/lib/session";

function refusal(status: number): NextResponse {
  return NextResponse.json({ message: SIGN_IN_FAILED_MESSAGE }, { status });
}

export async function POST(request: Request): Promise<Response> {
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return refusal(400);
  }

  const parsed = loginSchema.safeParse(raw);
  if (!parsed.success) {
    return refusal(400);
  }

  const result = await requestToken(parsed.data);
  if (!result.ok) {
    return refusal(401);
  }

  // The body carries no token: the bearer leaves this handler only as an
  // httpOnly cookie (`tests/login-route.test.ts`, "never returns the bearer
  // in the response body").
  const response = NextResponse.json({ ok: true });
  setSessionCookie(response, result.token);
  return response;
}
