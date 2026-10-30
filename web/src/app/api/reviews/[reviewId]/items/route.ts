/**
 * GET /api/reviews/[reviewId]/items: one page of a review's rows, proxied.
 * `limit` and the `after` cursor are each sent only when asked for, since the
 * tool refuses an empty one.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(
  request: Request,
  context: { params: Promise<{ reviewId: string }> },
): Promise<Response> {
  const { reviewId } = await context.params;
  const asked = new URL(request.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const name of ["limit", "after"]) {
    const value = asked.get(name);
    if (value !== null) {
      forwarded.set(name, value);
    }
  }
  const query = forwarded.size === 0 ? "" : `?${forwarded.toString()}`;
  return proxyAgentApi(`/reviews/${reviewId}/items${query}`, { method: "GET" });
}
