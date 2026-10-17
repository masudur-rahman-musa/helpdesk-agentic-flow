/**
 * POST /api/logout: expires the session cookie. The token is stateless, so
 * removing the only copy ends the session.
 */

import { NextResponse } from "next/server";

import { clearSessionCookie } from "@/lib/session";

export async function POST(): Promise<Response> {
  const response = NextResponse.json({ ok: true });
  clearSessionCookie(response);
  return response;
}
