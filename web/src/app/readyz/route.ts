/**
 * GET /readyz: ready only when agent-api answers, since every page is a read
 * through it. A static 200 would let the pod take traffic with nothing to serve.
 * A refused fetch and a non-2xx answer both give 503. The body never carries
 * upstream text, which would leak the internal agent-api address.
 */

import { READYZ_TIMEOUT_MS } from "@/lib/readyz-timeout";

/** Never cached and never prerendered: a probe answers for the live process. */
export const dynamic = "force-dynamic";

function agentApiBaseUrl(): string {
  return process.env.AGENT_API_URL ?? "http://localhost:8000";
}

export async function GET(): Promise<Response> {
  let upstreamOk = false;
  try {
    const response = await fetch(`${agentApiBaseUrl()}/readyz`, {
      method: "GET",
      cache: "no-store",
      signal: AbortSignal.timeout(READYZ_TIMEOUT_MS),
    });
    upstreamOk = response.ok;
  } catch {
    // Refusal, DNS failure and timeout all mean not ready.
    upstreamOk = false;
  }

  return Response.json(
    { status: upstreamOk ? "ready" : "unready" },
    { status: upstreamOk ? 200 : 503 },
  );
}
