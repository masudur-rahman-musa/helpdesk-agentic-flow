/**
 * POST /api/chat/stream: the streamed turn, proxied.
 *
 * The token lives only in the httpOnly cookie, so the turn goes out from here.
 * The body is checked before agent-api is called. The reply body is passed on
 * unread, so frames reach the browser as they arrive. A refusal uses
 * `{ code: "VALIDATION" }` so the chat can pick its message by code.
 */

import { NextResponse } from "next/server";

import { AGENT_API_STREAM_TIMEOUT_MS, proxyAgentApi } from "@/lib/agent-api";
import { chatMessageSchema } from "@/features/chat/schema";

function refusal(): NextResponse {
  return NextResponse.json({ code: "VALIDATION" }, { status: 400 });
}

export async function POST(request: Request): Promise<Response> {
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    // An unparseable body left to throw would be an untyped 500, which the
    // client cannot map to any sentence at all.
    return refusal();
  }

  const parsed = chatMessageSchema.safeParse(raw);
  if (!parsed.success) {
    return refusal();
  }

  return proxyAgentApi(
    "/chat/stream",
    {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify(parsed.data),
    },
    AGENT_API_STREAM_TIMEOUT_MS,
  );
}
