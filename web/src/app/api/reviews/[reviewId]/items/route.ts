/**
 * GET /api/reviews/[reviewId]/items: one review's rows, proxied. `limit` is
 * sent only when asked for, since the tool refuses an empty one.
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
