/**
 * GET /api/chat/chats: the signed-in person's chat list, proxied as it is.
 * Uses the short timeout, since this is a quick read, not a turn.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/chat/chats", { method: "GET" });
}
