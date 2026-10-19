/**
 * POST /api/admin/settings: the switch for the chat's tool steps box.
 *
 * The body is forwarded as it arrived. The MCP tool checks it, enforces access
 * and records who changed it; this route adds no second opinion.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** Same path as the browser's. */
const UPSTREAM_PATH = "/admin/settings";

export async function POST(request: Request): Promise<Response> {
  return proxyAgentApi(UPSTREAM_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: await request.text(),
  });
}
