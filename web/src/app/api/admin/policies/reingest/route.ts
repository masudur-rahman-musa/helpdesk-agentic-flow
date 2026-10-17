/**
 * POST /api/admin/policies/reingest: make one edited policy file live.
 *
 * The body is forwarded as it arrived, idempotency key included; making a key
 * here would turn a retry into a second ingest. It uses the longer reingest
 * timeout, since the first model load can take over ten seconds while the
 * ingest still finishes.
 */

import { AGENT_API_REINGEST_TIMEOUT_MS, proxyAgentApi } from "@/lib/agent-api";

/** Same path as the browser's. */
const UPSTREAM_PATH = "/admin/policies/reingest";

export async function POST(request: Request): Promise<Response> {
  return proxyAgentApi(
    UPSTREAM_PATH,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: await request.text(),
    },
    AGENT_API_REINGEST_TIMEOUT_MS,
  );
}
