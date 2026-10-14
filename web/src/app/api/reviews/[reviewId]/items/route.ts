/**
 * GET /api/reviews/[reviewId]/items — one review's rows, proxied.
 *
 * The same `proxyAgentApi` seam as the queue beside it, for the same reasons:
 * the bearer comes off the httpOnly cookie and an expired session clears it
 * and lands the browser on `/login`. Whether this caller may read the review
 * at all is the tool's scope check, never this route's.
 *
 * `limit` is forwarded when asked for, the same presence test the queue makes
 * and for the same reason — the tool's bound is 1..200, so an empty `limit=`
 * would be refused where no page size was asked for at all.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(
  request: Request,
  context: { params: Promise<{ reviewId: string }> },
): Promise<Response> {
  const { reviewId } = await context.params;
  const limit = new URL(request.url).searchParams.get("limit");
  const query = limit === null ? "" : `?${new URLSearchParams({ limit }).toString()}`;
  return proxyAgentApi(`/reviews/${reviewId}/items${query}`, { method: "GET" });
}
