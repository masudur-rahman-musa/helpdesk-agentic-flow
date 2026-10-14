/**
 * GET /api/me/access — every grant this person holds right now, proxied.
 *
 * The sibling of `/api/me/requests`, through the same seam and with the same
 * cookie and expiry handling. It sends no query at all: these rows are one
 * person's live grants, bounded by reality rather than by a page size, so
 * there is nothing to ask for.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/me/access", { method: "GET" });
}
