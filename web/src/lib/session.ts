/**
 * The session cookie, written, read and cleared in one place. The bearer lives
 * only here: `httpOnly` so no page script can read it, `SameSite=Lax` so it
 * skips cross-site requests. No expiry is set: the JWT's own `exp` is the only
 * lifetime, and a longer cookie would outlive its token.
 */

import { cookies } from "next/headers";
import type { NextResponse } from "next/server";

export const SESSION_COOKIE_NAME = "helpdesk_session";

/**
 * `Secure` unless the environment switches it off. Plain-http local
 * development sets `SESSION_COOKIE_SECURE=false` (see `.env.example`);
 * anything else, including an unset variable, is treated as secure, so a
 * deployment that forgets the variable gets the safe half.
 */
function cookieSecure(): boolean {
  return process.env.SESSION_COOKIE_SECURE !== "false";
}

export function setSessionCookie(response: NextResponse, token: string): void {
  response.cookies.set({
    name: SESSION_COOKIE_NAME,
    value: token,
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure: cookieSecure(),
  });
}

/**
 * Overwrites the cookie with an empty value and a past expiry. Either alone
 * leaves a live cookie.
 */
export function clearSessionCookie(response: NextResponse): void {
  response.cookies.set({
    name: SESSION_COOKIE_NAME,
    value: "",
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure: cookieSecure(),
    maxAge: 0,
  });
}

/** The bearer, for server-side code only. Never returned to the browser. */
export async function readSessionToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE_NAME)?.value ?? null;
}
