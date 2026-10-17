/**
 * GET /api/chat/messages: this session's transcript, proxied as it is. Uses
 * the short timeout, since this is a quick read, not a turn.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/chat/messages", { method: "GET" });
}
