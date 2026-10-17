/**
 * GET /api/admin/policies: this tenant's policy files, proxied as they are.
 * The MCP tool decides access and order.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** Same path as the browser's. */
const UPSTREAM_PATH = "/admin/policies";

export async function GET(): Promise<Response> {
  return proxyAgentApi(UPSTREAM_PATH, { method: "GET" });
}
