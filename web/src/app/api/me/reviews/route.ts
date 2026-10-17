/**
 * GET /api/me/reviews: the review queue waiting on this person, proxied.
 *
 * `status` and `limit` are forwarded only when present: an empty value would
 * be refused by the tool, so a page that asked for nothing would fail.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(request: Request): Promise<Response> {
  const asked = new URL(request.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const name of ["status", "limit"]) {
    const value = asked.get(name);
    if (value !== null) {
      forwarded.set(name, value);
    }
  }
  const query = forwarded.toString();
  return proxyAgentApi(`/me/reviews${query === "" ? "" : `?${query}`}`, { method: "GET" });
}
