/**
 * GET /api/me/approvals — the queue waiting on this person, proxied.
 *
 * The same seam as the other proxied reads: the bearer is attached from the
 * httpOnly cookie on the server, so it never reaches the browser, and an
 * expired one clears the cookie and lands the browser on `/login` rather than
 * leaving the page with an unexplained empty queue.
 *
 * Nothing is filtered, ordered or reshaped on the way through. Which requests
 * are waiting, which seat is the caller's and every name on a row are decided
 * by the MCP tool behind agent-api, which is also where RBAC, the kill switch
 * and the audit event apply.
 *
 * No `limit` is sent: the filter lives on the agent-api route, and the tool's
 * own default is the right page size for a queue nobody has asked to page.
 *
 * The ordinary timeout applies: this is a short read, not a turn.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/me/approvals", { method: "GET" });
}
