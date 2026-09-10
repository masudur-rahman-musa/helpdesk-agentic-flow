/**
 * GET /api/chat/messages — this session's transcript, proxied.
 *
 * The same seam as the stream, for the same reason: the bearer is attached
 * from the cookie here and an expired one lands the browser on `/login`
 * instead of leaving the page with an unexplained empty conversation.
 *
 * Nothing is filtered, ordered or reshaped on the way through. The window and
 * its order are agent-api's — `list_recent_messages` orders by `ordinal`, and
 * the read is bound to the token's tenant and session — so a second opinion
 * here could only disagree with the one that has the rows.
 *
 * The ordinary timeout applies: this is a short read, not a turn, so the
 * default `AGENT_API_TIMEOUT_MS` is the right bound and the streaming one
 * would leave a hung read holding a worker for two minutes.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/chat/messages", { method: "GET" });
}
