/**
 * GET /api/me/notifications — this person's own notifications.
 *
 * The same seam as every other proxied read: the bearer is attached from the
 * httpOnly cookie on the server, so it never reaches the browser, and an
 * expired one clears the cookie and lands the browser on `/login` rather than
 * leaving the bell showing a stale count it cannot refresh.
 *
 * Nothing is filtered, ordered, re-counted or reshaped on the way through.
 * Whose notifications these are is decided by the MCP tool behind agent-api,
 * which is also where RBAC, the kill switch and the self-only scope check
 * apply. The unread total arrives with the page because a bell counting the
 * rows it was handed would go quiet at the page size.
 *
 * `unread_only` and `limit` ARE forwarded (decision D, 23h's precedent): 23ga
 * put the unread filter on the tool because the bell's own view is the unread
 * set, and a proxy that could not ask for one would make the filter unreachable
 * from the only caller there is. Each parameter is sent only when it is present
 * — an empty `limit=` breaks the tool's 1..200 bound and an empty `unread_only=`
 * is not a bool, so writing both keys unconditionally would refuse a page that
 * asked for nothing. Present-but-empty is forwarded exactly as sent: the far
 * side decides, and rewriting it here would be a second opinion about a value
 * this route does not own.
 *
 * The ordinary timeout applies: this is a short read, not a turn.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(request: Request): Promise<Response> {
  const asked = new URL(request.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const name of ["unread_only", "limit"]) {
    const value = asked.get(name);
    if (value !== null) {
      forwarded.set(name, value);
    }
  }
  const query = forwarded.toString();
  return proxyAgentApi(`/me/notifications${query === "" ? "" : `?${query}`}`, { method: "GET" });
}
