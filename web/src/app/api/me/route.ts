/**
 * GET /api/me: who is signed in and which tabs they may see, proxied.
 *
 * The sibling of `/api/me/access`, through the same seam and with the same
 * cookie and expiry handling. It sends no query at all: the person is the
 * token's own and the tab list is computed server side from the RBAC matrix.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/me", { method: "GET" });
}
