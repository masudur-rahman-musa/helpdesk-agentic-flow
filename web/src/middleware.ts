/**
 * The session gate. Any app route but `/login` needs the session cookie;
 * without it the request is redirected to `/login`.
 *
 * `/login` is exempt, which is the infinite redirect loop this has to avoid —
 * a gate covering every path sends an unauthenticated visitor to `/login`
 * forever and the form never renders. Pinned by `tests/middleware.test.ts`'s
 * "does not redirect /login when no session cookie is present".
 *
 * The cookie's PRESENCE is all this checks. It is not a verification: the
 * bearer is verified by agent-api on every call it carries, and a stale or
 * forged cookie gets a 401 there, which the client turns back into a logout
 * (§1's second unhappy path). Putting a signature check here would be a second
 * verification path to keep in step with the Python one.
 *
 * `/api/*` is outside the matcher: `POST /api/login` is how a visitor with no
 * cookie gets one, so gating it would make signing in impossible.
 *
 * `/healthz` and `/readyz` are exempt for the same class of reason.
 * A probe carries no cookie, so the gate answered both with a 307 to `/login`:
 * compose's healthcheck reads `r.ok` and a kubelet reads the status, so the web
 * container was never healthy on a machine where nothing had signed in. They
 * are exempt rather than excluded from the matcher because the matcher is a
 * negative pattern, and a third alternative in it is harder to read than a
 * second entry in the set below.
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
