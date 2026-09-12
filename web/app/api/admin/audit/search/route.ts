/**
 * GET /api/admin/audit/search — the audit log, filtered, proxied.
 *
 * The same seam as every other proxied read: the bearer is attached from the
 * httpOnly cookie on the server, so it never reaches the browser, and an
 * expired one clears the cookie and lands the browser on `/login` rather than
 * leaving the page with an unexplained empty log — which on this surface would
 * read as "nothing happened" instead of "your session ended".
 *
 * Nothing is filtered, ordered or reshaped on the way through. Which events
 * this caller may see is decided by the MCP tool behind agent-api, which is
 * also where RBAC, the kill switch and the per-row scope filter apply.
 *
 * All eight filters ARE forwarded (decision D), each only when present: 13b put
 * them on the tool because an audit log is unusable without them, and this
 * route is the search box's only caller. The presence test is not decoration —
 * an empty `event_type=` is a filter nothing matches, an empty `limit=` breaks
 * the tool's 1-200 bound and an empty `occurred_from=` is not a date, so
 * writing the keys unconditionally would refuse a page that asked for nothing.
 *
 * The ordinary timeout applies: this is a short read, not a turn.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** `search_audit`'s own eight, in the order an earlier change section 1 lists them. */
const FILTERS = [
  "event_type",
  "subject_type",
  "subject_id",
  "actor_ref",
  "occurred_from",
  "occurred_to",
  "before_seq",
  "limit",
] as const;

export async function GET(request: Request): Promise<Response> {
  const asked = new URL(request.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const name of FILTERS) {
    const value = asked.get(name);
    if (value !== null) {
      forwarded.set(name, value);
    }
  }
  const query = forwarded.toString();
  return proxyAgentApi(`/admin/audit/search${query === "" ? "" : `?${query}`}`, { method: "GET" });
}
