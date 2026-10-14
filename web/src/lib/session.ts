/**
 * The session cookie: written, read and cleared in one place.
 *
 * The bearer lives ONLY here. It is `httpOnly`, so no
 * script on the page can read it, and `SameSite=Lax`, so it does not ride a
 * cross-site request. Both attributes are asserted literally, on the
 * serialised header rather than on the value, by `tests/login-route.test.ts`
 * ("serialises the session cookie with HttpOnly, SameSite=Lax and Path=/" and
 * "setSessionCookie writes the flags on the response it is given").
 *
 * No `Max-Age` and no `Expires` on the write: the cookie lasts the browser
 * session and the JWT's own `exp` is the only lifetime that decides anything.
 * A second, longer lifetime here would outlive the token it carries.
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
 * Overwrites the cookie with an empty value AND an expiry in the past. Either
 * half alone leaves a live cookie in the browser; pinned by
 * `tests/login-route.test.ts`'s "clearSessionCookie expires the cookie on the
 * response it is given".
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
