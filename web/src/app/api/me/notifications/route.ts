/**
 * GET /api/me/notifications: this person's own notifications, proxied.
 *
 * `unread_only` and `limit` are forwarded only when present: an empty value
 * would be refused by the tool, so a page that asked for nothing would fail.
 * The unread total comes from the server, since counting the rows on one page
 * would stop at the page size.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(request: Request): Promise<Response> {
  const asked = new URL(request.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const name of ["unread_only", "limit"]) {
    const value = asked.get(name);
    if (value !== null) {
      forwarded.set(name, value);
    }
  }
  const query = forwarded.toString();
  return proxyAgentApi(`/me/notifications${query === "" ? "" : `?${query}`}`, { method: "GET" });
}
