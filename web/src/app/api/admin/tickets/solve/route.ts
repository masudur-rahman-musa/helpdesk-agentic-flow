/**
 * POST /api/admin/tickets/solve: mark one open ticket solved.
 *
 * The body is forwarded as it arrived, idempotency key included; making a key
 * here would turn a retry into a second solve.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** Same path as the browser's. */
const UPSTREAM_PATH = "/admin/tickets/solve";

export async function POST(request: Request): Promise<Response> {
  return proxyAgentApi(UPSTREAM_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: await request.text(),
  });
}
