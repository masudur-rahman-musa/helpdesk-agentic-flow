/**
 * POST /api/login: trades the sign-in form for a session cookie.
 *
 * The body is checked before agent-api is called. Every refusal gets the same
 * message and no cookie, so the reply never says which input was wrong.
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

  // The token leaves only as an httpOnly cookie, never in the body.
  const response = NextResponse.json({ ok: true });
  setSessionCookie(response, result.token);
  return response;
}
