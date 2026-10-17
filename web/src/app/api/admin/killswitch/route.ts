/**
 * GET and POST /api/admin/killswitch: the board and the toggle on one path.
 *
 * The body is forwarded as it arrived. The MCP tool checks it, enforces access
 * and records who stopped the tool; this route adds no second opinion.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** Same path as the browser's. */
const UPSTREAM_PATH = "/admin/killswitch";

export async function GET(): Promise<Response> {
  return proxyAgentApi(UPSTREAM_PATH, { method: "GET" });
}

export async function POST(request: Request): Promise<Response> {
  return proxyAgentApi(UPSTREAM_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: await request.text(),
  });
}
