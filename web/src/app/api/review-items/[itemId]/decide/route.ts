/**
 * POST /api/review-items/[itemId]/decide — one keep or revoke.
 *
 * The same `proxyAgentApi` seam every other call uses, so the bearer comes off
 * the httpOnly cookie and an expired session clears it and lands the browser
 * on `/login`. A decision is a mutation, which is exactly why it goes through
 * the seam rather than around it: agent-api derives the idempotency key from
 * the verified reviewer, the item and the decision word, and the MCP tool
 * enforces RBAC, the scope rules, the kill switch and the audit event.
 *
 * The path is `/api/review-items/...` and not `/api/reviews/items/...`
 * (decision C): the second would put a static `items` segment beside the
 * dynamic `[reviewId]` branch next door, where only a framework precedence
 * rule keeps the two apart — a correctness nobody can see in the directory
 * listing.
 *
 * The body is forwarded VERBATIM as the bytes that arrived, with its content
 * type. The decision word is the TOOL's enum, so re-parsing and
 * re-serialising here would be a second opinion about a payload this route
 * does not validate — and the browser sends no idempotency key at all.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function POST(
  request: Request,
  context: { params: Promise<{ itemId: string }> },
): Promise<Response> {
  const { itemId } = await context.params;
  return proxyAgentApi(`/review-items/${itemId}/decide`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: await request.text(),
  });
}
