/**
 * GET /healthz — this process is up.
 *
 * Liveness, and nothing more: it reads no environment variable, opens no
 * socket and never touches agent-api. That is the whole point of splitting it
 * from `/readyz`. A liveness probe that checks a dependency gets the web
 * container RESTARTED for someone else's outage, and a restart loop is the
 * worst possible response to an upstream that is already struggling.
 *
 * Pinned by `tests/health-routes.test.ts`, "answers healthz with 200 when
 * nothing else is reachable", which stubs `fetch` to reject and asserts it was
 * never called.
 */

/** Never cached and never prerendered: a probe answers for the live process. */
export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  return Response.json({ status: "ok" });
}
