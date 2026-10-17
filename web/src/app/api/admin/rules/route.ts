/**
 * GET /api/admin/rules: this tenant's conflicting-role rules, proxied as they
 * are. The MCP tool decides access and order.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** Same path as the browser's. */
const UPSTREAM_PATH = "/admin/rules";

export async function GET(): Promise<Response> {
  return proxyAgentApi(UPSTREAM_PATH, { method: "GET" });
}
