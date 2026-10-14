/**
 * One label per member of `helpdesk.models.notification.NotificationKind`, in
 * one place.
 *
 * A separate module from `lib/review-labels.ts` and `lib/decision-labels.ts`,
 * that pair's own precedent: the notification family carries its own file, so a
 * `review_overdue` kind and a review STATUS of `open` never share a namespace
 * the next reader has to disambiguate by call site.
 *
 * Every key here is the enum's VALUE as `model_dump(mode="json")` puts it on
 * the wire (`grant_expired`), never the Python member name (`GRANT_EXPIRED`),
 * which is the failure `tests/notification-labels.test.ts` exists to catch: a
 * map keyed by member name misses on every lookup and the fallback makes it
 * silently green.
 *
 * The map is a HAND-KEPT mirror of the four members that enum declares, so a
 * member added on the Python side renders as its raw code with nothing red.
 * `tests/notification-labels.test.ts` lists all four as LITERALS and asserts
 * the key set equals them, so adding one is a deliberate edit to that list.
 *
 * A value no map knows answers THE VALUE ITSELF, `review-labels.ts`' decision
 * one module over: the raw code is greppable against the enum the moment
 * someone reports it, where a vague default would hide the drift. Pinned by
 * `tests/notification-labels.test.ts::renders the raw wire string`.
 *
 * A label says what happened and never what to do about it: the notification's
 * own body carries the sentence a person acts on, and this is the line above it.
 */

/** `helpdesk.models.notification.NotificationKind`, as its wire values. */
export const NOTIFICATION_KIND_LABELS: Record<string, string> = {
  grant_expired: "Access expired",
  grant_expiring: "Access expiring soon",
  review_overdue: "Review overdue",
  review_escalated: "Review escalated",
};

/** The label for a known kind, or the wire value itself for anything else. */
export function notificationKindLabel(kind: string): string {
  return NOTIFICATION_KIND_LABELS[kind] ?? kind;
}
