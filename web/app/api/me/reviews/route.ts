/**
 * GET /api/me/reviews — the review queue waiting on this person.
 *
 * The same seam as every other proxied read: the bearer is attached from the
 * httpOnly cookie on the server, so it never reaches the browser, and an
 * expired one clears the cookie and lands the browser on `/login` rather than
 * leaving the page with an unexplained empty queue.
 *
 * Nothing is filtered, ordered or reshaped on the way through. Which reviews
 * are waiting and how far along each one is are decided by the MCP tool behind
 * agent-api, which is also where RBAC, the kill switch and the scope checks
 * apply.
 *
 * `status` and `limit` ARE forwarded, which is the one place this surface
 * departs from 23b's and 23d's zero-argument proxies (decision D): 23e put the
 * status filter on the tool because a completed review is most of the table
 * after two quarters, and a proxy that could not ask for one would make the
 * filter unreachable from the only caller there is. Each parameter is sent
 * only when it is present — an empty `status=` reaches the tool's enum as an
 * unknown word and an empty `limit=` breaks its 1..200 bound, so writing both
 * keys unconditionally would refuse a page that asked for nothing.
 *
 * The ordinary timeout applies: this is a short read, not a turn.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(request: Request): Promise<Response> {
  const asked = new URL(request.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const name of ["status", "limit"]) {
    const value = asked.get(name);
    if (value !== null) {
      forwarded.set(name, value);
    }
  }
  const query = forwarded.toString();
  return proxyAgentApi(`/me/reviews${query === "" ? "" : `?${query}`}`, { method: "GET" });
}
