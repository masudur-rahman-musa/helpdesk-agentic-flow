/**
 * Labels for the review flag, status and decision values. Kept apart from
 * `decision-labels.ts`, whose `statusLabel` means request status. Keys are wire
 * values, not Python member names. An unknown value shows its raw code, so the
 * gap is easy to search for.
 */

/** `helpdesk.models.access.ReviewFlag`, in its precedence order. */
export const REVIEW_FLAG_LABELS: Record<string, string> = {
  sod_conflict: "Conflicts with other access",
  non_standard: "Outside the standard set",
  stale: "Expiring with no renewal",
  ok: "Nothing flagged",
};

/** `helpdesk.models.access.ReviewStatus`. A review is never cancelled. */
export const REVIEW_STATUS_LABELS: Record<string, string> = {
  open: "Open",
  completed: "Completed",
};

/** `helpdesk.models.access.ReviewDecision`, as what a reviewer already did. */
export const REVIEW_DECISION_LABELS: Record<string, string> = {
  keep: "Kept",
  revoke: "Revoked",
};

/** The label for a known member, or the wire value itself for anything else. */
function labelled(labels: Record<string, string>, value: string): string {
  return labels[value] ?? value;
}

export function reviewFlagLabel(flag: string): string {
  return labelled(REVIEW_FLAG_LABELS, flag);
}

export function reviewStatusLabel(status: string): string {
  return labelled(REVIEW_STATUS_LABELS, status);
}

export function reviewItemDecisionLabel(decision: string): string {
  return labelled(REVIEW_DECISION_LABELS, decision);
}
