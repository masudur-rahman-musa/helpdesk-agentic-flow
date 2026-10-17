/**
 * GET /api/me/approvals: the queue waiting on this person, proxied as it is.
 * The MCP tool decides which requests and seats are theirs.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/me/approvals", { method: "GET" });
}
