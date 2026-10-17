/** GET /api/me/requests: every request this person has filed, proxied as it is. */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/me/requests", { method: "GET" });
}
