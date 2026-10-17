import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { type PendingApproval, pendingApprovalsSchema } from "@/features/approvals/schema";
import { API, apiPath } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

export function useApprovals() {
  return useQuery({ queryKey: queryKeys.myApprovals, queryFn: fetchApprovals });
}

/** A success re-reads the queue; the server decides what the row becomes. */
export function useApproveRequest(requestId: string) {
  const invalidate = useRefreshApprovals();
  return useMutation({
    mutationFn: (comment: string | null) => postDecision(requestId, "approve", { comment }),
    onSuccess: invalidate,
  });
}

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

/** Carries only the typed code; the page picks the sentence. */
export class DecisionRefusedError extends Error {
  readonly code: unknown;

  constructor(code: unknown) {
    super("the decision was refused");
    this.code = code;
  }
}

/** Empty while the browser follows the redirect to `/login`. */
async function fetchApprovals(): Promise<PendingApproval[]> {
  const parsed = await readWire(API.myApprovals, pendingApprovalsSchema);
  return parsed === null ? [] : parsed.approvals;
}

/**
 * No idempotency key is sent: the server builds it from approver, request and
 * verb, so a double click replays instead of filing twice.
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
