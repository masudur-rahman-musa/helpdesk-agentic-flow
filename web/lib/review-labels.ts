/**
 * One label per member of the review family's three vocabularies, in one place
 *.
 *
 * A separate module from `lib/decision-labels.ts` on purpose: that file's
 * `statusLabel` already means REQUEST status, and a second `open`/`completed`
 * status in the same namespace is a collision the next reader has to
 * disambiguate by call site. `reviews-schema.ts` is the same precedent one
 * layer down — the review family carries its own file.
 *
 * Every key here is the enum's VALUE as `model_dump(mode="json")` puts it on
 * the wire (`sod_conflict`, `completed`, `keep`), never the Python member name,
 * which is the failure `tests/review-labels.test.ts` exists to catch: a map
 * keyed by member name misses on every lookup and the fallback makes it
 * silently green.
 *
 * The three maps are a HAND-KEPT mirror of three Python enums in
 * `helpdesk/models/access.py`, so a member added on the Python side renders as
 * its raw code with nothing red. `tests/review-labels.test.ts` lists all eight
 * members as LITERALS and asserts each map's key set equals its enum's, so
 * adding one is a deliberate edit to that list.
 *
 * A value no map knows answers THE VALUE ITSELF, `decision-labels.ts`'s
 * decision B one module over: the raw code is greppable against the enum the
 * moment someone reports it, where a vague default would hide the drift.
 * Pinned by `::an unknown value in any of the three maps renders the raw wire
 * string`.
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
