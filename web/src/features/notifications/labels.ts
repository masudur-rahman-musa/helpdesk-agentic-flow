/**
 * One label per notification kind, keyed by the wire value (`grant_expired`),
 * not the Python member name. An unknown kind shows its raw code, so the gap is
 * easy to search for instead of hidden behind a vague default.
 */

export const NOTIFICATION_KIND_LABELS: Record<string, string> = {
  grant_expired: "Access expired",
  grant_expiring: "Access expiring soon",
  review_overdue: "Review overdue",
  review_escalated: "Review escalated",
  approval_needed: "Approval needed",
  request_progress: "Request update",
  request_granted: "Access granted",
  request_rejected: "Request rejected",
  request_failed: "Request failed",
  request_cancelled: "Request cancelled",
  access_revoked: "Access removed",
};

/** The label for a known kind, or the wire value itself for anything else. */
export function notificationKindLabel(kind: string): string {
  return NOTIFICATION_KIND_LABELS[kind] ?? kind;
}
