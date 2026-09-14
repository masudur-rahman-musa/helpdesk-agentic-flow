/**
 * POST /api/admin/policies/reingest — make one edited policy file live (31a).
 *
 * Its own file mirroring the upstream path (decision I), not a second verb on
 * `/api/admin/policies`: 24cb put two verbs on one file because both hit ONE
 * upstream path, and here the upstream paths differ. 24eb decision C's rule is
 * that the browser's path equals agent-api's, so a POST on the board's file
 * would have to name a path that is not its own.
 *
 * The same `proxyAgentApi` seam every other proxied call uses, so the bearer
 * comes off the httpOnly cookie on the server and never reaches the browser,
 * and an expired one clears the cookie and lands the browser on `/login`.
 *
 * This is a MUTATION, which is exactly why it goes through the seam rather
 * than around it: nothing is decided here or in agent-api. The MCP tool
 * enforces RBAC and the kill switch, the pipeline spends the idempotency key,
 * and the `policy.reingested` event names the admin who re-ingested.
 *
 * The body is forwarded VERBATIM as the bytes that arrived, with its content
 * type. `slug` and `version` are checked against the corpus store's own
 * constants inside `helpdesk.rag.naming`, and the idempotency key is the
 * caller's, so re-parsing and re-serialising here would be a second opinion
 * about a payload this route does not validate — and minting a key here would
 * turn a retry into a second ingest rather than a replay.
 *
 * Nothing is filtered, ordered or reshaped on the way through: row 31b parses
 * the tool's own envelope with `policyReingestResultSchema`, and a key added
 * here would leave that parser refusing a payload agent-api answered
 * correctly.
 *
 * The ordinary timeout applies. A re-ingest is a gateway call plus an embed
 * pass rather than a turn, and an earlier change §5 records that the process's first
 * model load can outrun this bound while the ingest itself completes; the
 * longer timeout belongs with the control that calls this, in row 31b.
 */

import { proxyAgentApi } from "@/lib/agent-api";

/** agent-api's own path, which decision I keeps identical to the browser's. */
const UPSTREAM_PATH = "/admin/policies/reingest";

export async function POST(request: Request): Promise<Response> {
  return proxyAgentApi(UPSTREAM_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: await request.text(),
  });
}
