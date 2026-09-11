/**
 * POST /api/approvals/[requestId]/reject — one rejection, proxied.
 *
 * The approve route's twin, and separate on purpose: the verb is part of the
 * idempotency key agent-api derives, so one endpoint taking a decision as a
 * field would let an Approve and a Reject on one request collapse into
 * whichever was filed first.
 *
 * The body is forwarded VERBATIM as the bytes that arrived. The reason's
 * bounds are the tool's (1..2000, decision E), so an empty reason crosses the
 * wire as it was typed and comes back as the typed `VALIDATION` the page has a
 * sentence for — a proxy that dropped it to `null` would have the tool report
 * a missing field instead.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function POST(
  request: Request,
  context: { params: Promise<{ requestId: string }> },
): Promise<Response> {
  const { requestId } = await context.params;
  return proxyAgentApi(`/approvals/${requestId}/reject`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: await request.text(),
  });
}
