/** GET /api/me/access: every grant this person holds now, proxied. */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/me/access", { method: "GET" });
}
