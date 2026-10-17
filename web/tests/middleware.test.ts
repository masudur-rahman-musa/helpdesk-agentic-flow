// @vitest-environment node
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { middleware } from "@/middleware";
import { SESSION_COOKIE_NAME } from "@/lib/session";

/**
 * The session gate ("a request to any app route other than
 * /login carrying no valid session cookie is redirected to /login").
 *
 * NextResponse.redirect() answers 307, which is the value pinned below.
 */
const REDIRECT_STATUS = 307;

function requestFor(path: string, options: { withCookie: boolean }): NextRequest {
  const request = new NextRequest(new URL(`http://localhost${path}`));
  if (options.withCookie) {
    request.cookies.set(SESSION_COOKIE_NAME, "header.payload.signature");
  }
  return request;
}

describe("middleware — unauthenticated app routes", () => {
  const guarded: ReadonlyArray<{ path: string }> = [{ path: "/chat" }, { path: "/requests" }];

  it.each(guarded)("redirects $path to /login when no session cookie is present", ({ path }) => {
    // Kills a middleware with no gate at all, and kills one whose matcher only
    // covers /chat: /requests is an app route the same rule must reach, so a
    // /chat-shaped condition passes the first row and fails the second.
    const response = middleware(requestFor(path, { withCookie: false }));
    expect(response.status).toBe(REDIRECT_STATUS);
    const location = response.headers.get("location");
    expect(location).not.toBeNull();
    expect(location?.endsWith("/login")).toBe(true);
  });
});

describe("middleware — authenticated app routes", () => {
  it("does not redirect /chat when the session cookie is present", () => {
    // Kills a gate that redirects unconditionally (the "always send to /login"
    // inversion), which the unauthenticated rows above cannot tell apart from a
    // working gate. Same path, same middleware, cookie the only difference.
    const response = middleware(requestFor("/chat", { withCookie: true }));
    expect(response.headers.get("location")).toBeNull();
  });
});

describe("middleware — the probe routes are never gated", () => {
  const probes: ReadonlyArray<{ path: string }> = [{ path: "/healthz" }, { path: "/readyz" }];

  it.each(probes)("does not redirect $path when no session cookie is present", ({ path }) => {
    // Kills the gate that sent both probes to /login with a 307: a
    // probe carries no cookie, compose's healthcheck reads `r.ok` and a kubelet
    // reads the status, so a gated /healthz means the container is never
    // healthy on a machine where nobody has signed in. Deleting either entry
    // from PUBLIC_PATHS fails the matching row here; the /login case above
    // cannot see it, because /login was exempt the whole time.
    const response = middleware(requestFor(path, { withCookie: false }));
    expect(response.status).not.toBe(REDIRECT_STATUS);
    expect(response.headers.get("location")).toBeNull();
  });
});

describe("middleware — /login is never the target of its own redirect", () => {
  it("does not redirect /login when no session cookie is present", () => {
    // Kills the infinite redirect loop: a gate that guards every path including
    // /login sends an unauthenticated visitor to /login forever, and the browser
    // never renders the form. No other case in this file sees it.
    const response = middleware(requestFor("/login", { withCookie: false }));
    expect(response.headers.get("location")).toBeNull();
  });
});
