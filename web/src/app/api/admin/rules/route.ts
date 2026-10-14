/**
 * GET /api/admin/rules — this tenant's SoD rules, proxied.
 *
 * The same seam as every other proxied read: the bearer is attached from the
 * httpOnly cookie on the server, so it never reaches the browser, and an
 * expired one clears the cookie and lands the browser on `/login` rather than
 * leaving the page with an empty board — which on this surface would read as
 * "this tenant has no conflicting roles" instead of "your session ended".
 *
 * Nothing is filtered, ordered or reshaped on the way through. Which rules this
 * caller may read is decided by the MCP tool behind agent-api, which is also
 * where RBAC, the kill switch and the audit event apply, and the order is the
 * tool's (24ea decision C) so 24f and every later caller read one sequence.
 *
 * No query string and no body: the upstream route takes no parameter at all, so
 * there is nothing here to forward and nothing for a browser to name.
 *
 * The ordinary timeout applies: this is a short read, not a turn.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** agent-api's own path, which decision C keeps identical to the browser's. */
const UPSTREAM_PATH = "/admin/rules";

export async function GET(): Promise<Response> {
  return proxyAgentApi(UPSTREAM_PATH, { method: "GET" });
}
