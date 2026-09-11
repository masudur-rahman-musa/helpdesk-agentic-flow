/**
 * POST /api/approvals/[requestId]/approve — one approval, proxied.
 *
 * The same `proxyAgentApi` seam every other call uses, so the bearer comes off
 * the httpOnly cookie and an expired session clears it and lands the browser
 * on `/login`. A decision is a mutation, which is exactly why it goes through
 * the seam rather than around it: agent-api derives the idempotency key, the
 * MCP tool enforces RBAC, the seat rules and the kill switch, and the audit
 * event names the request this person decided.
 *
 * The body is forwarded VERBATIM as the bytes that arrived, with its content
 * type. The comment's bound is the tool's, so re-parsing
 * and re-serialising here would be a second opinion about a payload this route
 * does not validate — and the browser sends no idempotency key at all, because
 * agent-api derives it from the approver, the request and the verb.
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
