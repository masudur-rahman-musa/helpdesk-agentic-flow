/**
 * GET /api/admin/audit/verify: is this tenant's audit chain intact?
 *
 * Takes no arguments: agent-api reads the tenant from the verified token. A
 * 403 passes through as a 403, so a user without audit rights is told they may
 * not ask, not that the chain is down.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/admin/audit/verify", { method: "GET" });
}
