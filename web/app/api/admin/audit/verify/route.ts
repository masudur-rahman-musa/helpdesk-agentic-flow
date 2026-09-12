/**
 * GET /api/admin/audit/verify — is this tenant's audit chain intact?
 *
 * An earlier change's route, reached from the browser for the first time. It is the one
 * read in this diff that does NOT cross the MCP boundary: agent-api walks the
 * chain itself, against the tenant in the verified token and never a parameter
 *. So this handler takes no argument at all and forwards no
 * query and no body — the zero-argument shape 23b's proxies use, here because
 * the route upstream genuinely has nothing to ask for.
 *
 * A non-200 that is not a 401 is passed through with its status and its typed
 * body (§5's fifth risk): an earlier change answers **403** to a verified token without
 * an audit principal, and a proxy that flattened it into `proxyAgentApi`'s 502
 * would tell an ordinary employee the chain is unreachable — which reads as an
 * outage, or as tampering — rather than that they may not ask. `proxyAgentApi`
 * already behaves that way; the case in `tests/audit-proxy-routes.test.ts` is
 * what keeps a later hand from adding an `upstream.ok` check here.
 *
 * The 401, the cleared cookie and the 502 on a transport failure are that same
 * shared seam, unchanged.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function GET(): Promise<Response> {
  return proxyAgentApi("/admin/audit/verify", { method: "GET" });
}
