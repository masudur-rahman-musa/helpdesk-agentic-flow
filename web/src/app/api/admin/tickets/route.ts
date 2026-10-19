/**
 * GET /api/admin/tickets: every open ticket in this tenant, proxied as it is.
 * The search string (an optional `limit`) is passed on; the MCP tool decides
 * access and order.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** Same path as the browser's. */
const UPSTREAM_PATH = "/admin/tickets";

export async function GET(request: Request): Promise<Response> {
  const { search } = new URL(request.url);
  return proxyAgentApi(`${UPSTREAM_PATH}${search}`, { method: "GET" });
}
