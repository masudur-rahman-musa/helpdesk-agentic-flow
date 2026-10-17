/**
 * POST /api/approvals/[requestId]/approve: one approval, proxied.
 *
 * The body is forwarded as it arrived. The browser sends no idempotency key:
 * agent-api builds it from the approver, the request and the verb.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function POST(
  request: Request,
  context: { params: Promise<{ requestId: string }> },
): Promise<Response> {
  const { requestId } = await context.params;
  return proxyAgentApi(`/approvals/${requestId}/approve`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: await request.text(),
  });
}
