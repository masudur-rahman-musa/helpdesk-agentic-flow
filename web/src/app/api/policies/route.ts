/**
 * GET /api/policies?slug=...: the company's access rules, proxied as they are.
 * The slug is passed on encoded and unread; the tool checks it and answers
 * `VALIDATION` or `NOT_FOUND`. With no slug it answers the list alone.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(request: Request): Promise<Response> {
  const slug = new URL(request.url).searchParams.get("slug");
  const query = slug === null ? "" : `?slug=${encodeURIComponent(slug)}`;
  return proxyAgentApi(`/policies${query}`, { method: "GET" });
}
