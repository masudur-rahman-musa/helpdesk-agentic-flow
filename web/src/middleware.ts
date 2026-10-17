/**
 * The session check. Every app route but the public ones needs the session
 * cookie, or it is sent to `/login`. Only the cookie's presence is checked:
 * agent-api checks the bearer on every call, and a 401 there logs the user out.
 * `/login` is public, or the redirect would loop. `/api/*` is outside the
 * matcher, since `POST /api/login` is how a cookie is obtained. `/healthz` and
 * `/readyz` are public because health probes carry no cookie.
 */

import { NextResponse, type NextRequest } from "next/server";

import { SESSION_COOKIE_NAME } from "@/lib/session";

const PUBLIC_PATHS: ReadonlySet<string> = new Set(["/login", "/healthz", "/readyz"]);

export function middleware(request: NextRequest): NextResponse {
  if (PUBLIC_PATHS.has(request.nextUrl.pathname)) {
    return NextResponse.next();
  }

  if (request.cookies.get(SESSION_COOKIE_NAME) !== undefined) {
    return NextResponse.next();
  }

  const target = request.nextUrl.clone();
  target.pathname = "/login";
  target.search = "";
  return NextResponse.redirect(target);
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
