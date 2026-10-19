/** GET /api/me/tickets: the tickets this person has opened, proxied as it is. */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/me/tickets", { method: "GET" });
}
