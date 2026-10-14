/**
 * POST /api/logout — expires the session cookie and nothing else.
 *
 * There is no upstream call to make: the bearer is stateless (HS256, verified
 * per request), so ending the session means removing the only copy of it.
 * Pinned by `tests/login-route.test.ts`'s "clears the session cookie with an
 * expiry in the past".
 */

import { NextResponse } from "next/server";

import { clearSessionCookie } from "@/lib/session";

export async function POST(): Promise<Response> {
  const response = NextResponse.json({ ok: true });
  clearSessionCookie(response);
  return response;
}
