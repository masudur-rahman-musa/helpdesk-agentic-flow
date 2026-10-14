/**
 * GET /readyz — this process can serve traffic.
 *
 * Readiness here means the one dependency the web server actually has is
 * answering: every page this app renders is a proxied read through
 * `lib/agent-api.ts`, so a web pod whose agent-api is down has nothing to
 * serve. Decision C2, a static 200, was rejected for exactly that reason: it
 * makes the probe a decoration, and the pod takes traffic while its only
 * upstream is unreachable.
 *
 * The body names a status and nothing else. Neither the upstream body nor the
 * caught error text is forwarded, because both carry the internal agent-api
 * address and this path is reachable by anyone who can reach the pod. Pinned by
 * `tests/health-routes.test.ts`, "never puts the agent-api url in the readyz
 * body".
 *
 * Both failure shapes answer 503: a refused connection (the fetch rejects) and
 * a reachable agent-api that reports itself unready (a non-2xx status). The
 * second is the one a bare `try/catch` misses, and it is pinned separately by
 * "answers readyz with 503 when agent-api answers a non-2xx status".
 *
 * `AGENT_API_URL` is read per call rather than at module load, the same
 * convention `lib/agent-api.ts` documents, so a reconfigured process and a test
 * that sets the variable per case both see the change.
 *
 * The timeout is imported from `lib/readyz-timeout.ts` rather than declared
 * here: a route module may export only handlers and Next's own config fields,
 * and the test that pins the value needs to import it.
 */

import { READYZ_TIMEOUT_MS } from "@/lib/readyz-timeout";

/** Never cached and never prerendered: a probe answers for the live process. */
export const dynamic = "force-dynamic";

/** The same default as `lib/agent-api.ts`, read per call for the same reason. */
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
    // A refusal, a DNS failure and the timeout above are one answer: not ready.
    // The error is deliberately not read; see the note on the body above.
    upstreamOk = false;
  }

  return Response.json(
    { status: upstreamOk ? "ready" : "unready" },
    { status: upstreamOk ? 200 : 503 },
  );
}
