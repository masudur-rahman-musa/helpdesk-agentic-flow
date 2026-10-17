/**
 * GET /api/admin/audit/search: the filtered audit log, proxied.
 *
 * Each filter is forwarded only when present: an empty `limit=` or
 * `occurred_from=` would be refused by the tool, so sending every key would
 * break a page that asked for nothing. Access is decided by the MCP tool.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** The filters `search_audit` takes. */
const FILTERS = [
  "event_type",
  "subject_type",
  "subject_id",
  "actor_ref",
  "occurred_from",
  "occurred_to",
  "before_seq",
  "limit",
] as const;

export async function GET(request: Request): Promise<Response> {
  const asked = new URL(request.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const name of FILTERS) {
    const value = asked.get(name);
    if (value !== null) {
      forwarded.set(name, value);
    }
  }
  const query = forwarded.toString();
  return proxyAgentApi(`/admin/audit/search${query === "" ? "" : `?${query}`}`, { method: "GET" });
}
