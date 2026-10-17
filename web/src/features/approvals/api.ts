import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { type PendingApproval, pendingApprovalsSchema } from "@/features/approvals/schema";
import { API, apiPath } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** The queue waiting on this approver, read through TanStack Query. */
export function useApprovals() {
  return useQuery({ queryKey: queryKeys.myApprovals, queryFn: fetchApprovals });
}

/**
 * Approve one request, with an optional comment. A success re-reads the queue
 * and removes nothing on its own: the tool decides what the row becomes
 *.
 */
export function useApproveRequest(requestId: string) {
  const invalidate = useRefreshApprovals();
  return useMutation({
    mutationFn: (comment: string | null) => postDecision(requestId, "approve", { comment }),
    onSuccess: invalidate,
  });
}

/** Reject one request with its reason. A success re-reads the queue. */
export function useRejectRequest(requestId: string) {
  const invalidate = useRefreshApprovals();
  return useMutation({
    mutationFn: (reason: string) => postDecision(requestId, "reject", { reason }),
    onSuccess: invalidate,
  });
}

function useRefreshApprovals(): () => Promise<void> {
  const queryClient = useQueryClient();
  return async () => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.myApprovals });
  };
}

/**
 * A decision the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
export class DecisionRefusedError extends Error {
  readonly code: unknown;

  constructor(code: unknown) {
    super("the decision was refused");
    this.code = code;
  }
}

/**
 * The queue waiting on this approver, or the empty list while the browser
 * follows the proxy's redirect to `/login`.
 *
 * A refused read throws a `WireRefusedError` and this page dresses it with
 * nothing (24ia decision B): the region's own read alert is what an approver
 * sees for a typed 403 as well as for a bodyless 500, and the per-code
 * sentences on this surface stay the DECISION's alone. Pinned by
 * `tests/approvals-page.test.tsx::a 403 FORBIDDEN on the queue renders the read
 * alert, not the code's sentence`.
 */
async function fetchApprovals(): Promise<PendingApproval[]> {
  const parsed = await readWire(API.myApprovals, pendingApprovalsSchema);
  return parsed === null ? [] : parsed.approvals;
}

/**
 * One decision, posted to the proxy for this request and this verb.
 *
 * No idempotency key is sent: agent-api derives it from the approver, the
 * request and the verb (23c decision B), so a double-clicked Approve replays
 * the stored response instead of filing a second decision.
 */
async function postDecision(
  requestId: string,
  verb: "approve" | "reject",
  body: Record<string, unknown>,
): Promise<void> {
  const response = await fetch(
    apiPath(verb === "approve" ? API.approveRequest : API.rejectRequest, { requestId }),
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    },
  );

  if (response.redirected) {
    window.location.assign(response.url);
    return;
  }
  if (!response.ok) {
    const refused: unknown = await response.json().catch(() => null);
    const code =
      typeof refused === "object" && refused !== null
        ? (refused as Record<string, unknown>).code
        : undefined;
    throw new DecisionRefusedError(code);
  }
}
