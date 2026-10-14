/**
 * POST /api/chat/stream — the streamed turn, proxied.
 *
 * The browser never talks to agent-api: the bearer lives only in the httpOnly
 * cookie (22a decision A), so the turn goes out from here with the token
 * attached by `proxyAgentApi` and comes back through the same seam, which is
 * what makes an expired session ONE redirect to `/login` rather than a 401 the
 * chat page cannot recover from.
 *
 * Order, as `app/api/login/route.ts` already has it: parse, pre-check against
 * the shared schema, and only then call agent-api.
 * `tests/chat-stream-route.test.ts` primes the fetch stub to SUCCEED and
 * asserts the call count for each row of the boundary table, which is the only
 * assertion that can tell this order from the other one.
 *
 * The upstream body is handed on as a body, never read: `proxyAgentApi`
 * constructs its response from `upstream.body`, so the frames reach the client
 * as they arrive. Pinned by "delivers the first frame while the upstream
 * stream is still open" — a buffering proxy passes every settled-reply test
 * there is (§5's first risk).
 *
 * A refusal here answers `{ code: "VALIDATION" }`, agent-api's own envelope,
 * and NOT 22a's `{ message }`: the chat client picks its sentence from
 * `FAILURE_MESSAGES` by code, and a body without one leaves it rendering the
 * default for a failure it could have named.
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
