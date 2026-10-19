/**
 * GET /api/chat/messages?chat_id=...: one chat's transcript, proxied as it is.
 * The id is passed on encoded and unread; agent-api checks it and answers
 * `VALIDATION` or `NOT_FOUND`. Uses the short timeout, since this is a quick
 * read, not a turn.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(request: Request): Promise<Response> {
  const chatId = new URL(request.url).searchParams.get("chat_id");
  const query = chatId === null ? "" : `?chat_id=${encodeURIComponent(chatId)}`;
  return proxyAgentApi(`/chat/messages${query}`, { method: "GET" });
}
