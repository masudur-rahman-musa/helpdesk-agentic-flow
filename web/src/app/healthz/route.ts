/**
 * GET /healthz: this process is up, and nothing more. It never calls agent-api:
 * a liveness check that tests a dependency gets the container restarted for
 * someone else's outage.
 */

/** Never cached: a probe answers for the live process. */
export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  return Response.json({ status: "ok" });
}
