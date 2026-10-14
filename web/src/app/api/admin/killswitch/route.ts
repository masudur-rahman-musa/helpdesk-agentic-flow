/**
 * GET and POST /api/admin/killswitch — the board and the toggle.
 *
 * One path carrying both verbs (decision E): the board and the toggle are one
 * resource and one page, so this is one bearer seam to keep in step instead of
 * two.
 *
 * The same `proxyAgentApi` seam every other proxied call uses, so the bearer
 * comes off the httpOnly cookie on the server and never reaches the browser,
 * and an expired one clears the cookie and lands the browser on `/login`
 * rather than leaving the page with a board that looks like a platform with no
 * tools.
 *
 * The toggle is a MUTATION, which is exactly why it goes through the seam
 * rather than around it: agent-api derives nothing and decides nothing, the
 * MCP tool enforces RBAC and the protected-tool guard, and the audit event
 * names the admin who stopped the tool.
 *
 * The body is forwarded VERBATIM as the bytes that arrived, with its content
 * type. `tool_name`'s and `reason`'s bounds are the tool's own constants and
 * `scope`'s enum is agent-api's, so re-parsing and re-serialising here would
 * be a second opinion about a payload this route does not validate — and the
 * browser sends no tenant id and no idempotency key at all.
 *
 * Nothing is filtered, ordered or reshaped on the way through: 24d parses the
 * tool's own envelopes, and a key added here would leave
 * `killswitch-schema.ts` refusing a payload agent-api answered correctly.
 *
 * The ordinary timeout applies: both calls are short, neither is a turn.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** agent-api's own path, which decision E keeps identical to the browser's. */
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
