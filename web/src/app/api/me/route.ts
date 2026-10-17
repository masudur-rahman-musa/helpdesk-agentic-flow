/**
 * GET /api/me: who is signed in and which tabs they may see. The tab list is
 * worked out on the server from the access rules.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/me", { method: "GET" });
}
