/**
 * POST /api/review-items/[itemId]/decide: one keep or revoke, proxied.
 *
 * Not under `/api/reviews/`, where a fixed `items` folder beside the
 * `[reviewId]` folder would rely on a hidden framework rule. The body is
 * forwarded as it arrived; agent-api builds the idempotency key.
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
