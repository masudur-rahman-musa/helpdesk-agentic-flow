/**
 * GET /api/me/requests — every request this person has filed, proxied.
 *
 * The same seam as the chat routes, for the same reasons: the bearer is
 * attached from the httpOnly cookie on the server, so it never reaches the
 * browser, and an expired one clears the cookie and lands the browser on
 * `/login` rather than leaving the page with an unexplained empty list.
 *
 * Nothing is filtered, ordered or reshaped on the way through. The page size,
 * the order and every name on a row are decided by the MCP tool behind
 * agent-api, which is also where RBAC, the kill switch and the audit event
 * apply — a second opinion here could only disagree with the one that has the
 * rows.
 *
 * The ordinary timeout applies: this is a short read, not a turn, so
 * `proxyAgentApi`'s default bound is the right one.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/me/requests", { method: "GET" });
}
