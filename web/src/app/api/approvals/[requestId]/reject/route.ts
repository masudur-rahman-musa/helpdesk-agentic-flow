/**
 * POST /api/approvals/[requestId]/reject: one rejection, proxied.
 *
 * Separate from approve on purpose: the verb is part of the idempotency key, so
 * an Approve and a Reject on one request can never merge into one.
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
