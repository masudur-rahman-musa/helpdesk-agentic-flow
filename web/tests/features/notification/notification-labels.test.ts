import { describe, expect, it } from "vitest";

import { NOTIFICATION_KIND_LABELS, notificationKindLabel } from "@/features/notifications/labels";

/**
 * `notification-labels.test.ts`: the hand-kept mirror of
 * `helpdesk.models.notification.NotificationKind`, as literals. Pure — no DOM,
 * no fetch, no React. (The neighbours are `notifications-schema.test.ts` and
 * `notifications-proxy-routes.test.ts`.)
 *
 * Every key asserted here is the enum's VALUE as `model_dump(mode="json")` puts
 * it on the wire (`grant_expired`), never the Python member name.
 *
 * Shapes this file fixes: each of the four wire
 * values renders a sentence, but none is written of the four sentences down, so
 * no case here pins a TEXT — the strongest true statement available is "a
 * non-empty label that is not the wire code itself", plus the map/lookup
 * agreement below. `review-labels.test.ts` made exactly the same call one
 * module over, for the same reason: inventing the four sentences here would
 * leave the implementer free to invent a fifth answer this file happens not to
 * contradict.
 */

/** Every `helpdesk.models.notification.NotificationKind` member, as its wire value. */
const KINDS = [
  "grant_expired",
  "grant_expiring",
  "review_overdue",
  "review_escalated",
  "approval_needed",
  "request_progress",
  "request_granted",
  "request_rejected",
  "request_failed",
  "access_revoked",
  "request_cancelled",
] as const;

/** The six request-flow labels, pinned as text. */
const REQUEST_LABELS = {
  approval_needed: "Approval needed",
  request_progress: "Request update",
  request_granted: "Access granted",
  request_rejected: "Request rejected",
  request_failed: "Request failed",
  access_revoked: "Access removed",
} as const;

describe("every NotificationKind member", () => {
  it.each(KINDS)("%s reads as a label, not as its code", (kind) => {
    // Four members, one assertion each. Catches a map short of a member — a map
    // written from the two kinds a reader remembers renders `review_escalated`
    // through the fallback, which is silently green everywhere else — and
    // catches a map keyed by the Python MEMBER NAME (`GRANT_EXPIRED`), where
    // every lookup misses and the page shows the wire value at a person. The
    // `not.toBe(kind)` half is load-bearing: a lookup implemented as the
    // identity function (the fallback with no map behind it) satisfies
    // "non-empty" for all four.
    const label = notificationKindLabel(kind);

    expect(label.length).toBeGreaterThan(0);
    expect(label).not.toBe(kind);
  });
});

describe("the six request-flow kinds", () => {
  it.each(Object.entries(REQUEST_LABELS))("%s reads as %s", (kind, label) => {
    // Catches a label that is non-empty but wrong or vague (for example every
    // new kind sharing "Notification"), which the generic case above allows.
    expect(notificationKindLabel(kind)).toBe(label);
  });
});

describe("the map and the lookup", () => {
  it("holds exactly the eleven NotificationKind members", () => {
    // The mirror rule in the one direction the case above cannot see: a map
    // carrying a kind Python does NOT have is a vocabulary that has silently
    // drifted from `helpdesk/models/notification.py`, and a page rendering it
    // would name a state the scheduler can never write. Adding a member on the
    // Python side is then a deliberate edit to `KINDS` at the top of this file
    // rather than a change nothing notices.
    expect(Object.keys(NOTIFICATION_KIND_LABELS).sort()).toEqual([...KINDS].sort());
  });

  it("answers its own map's value for every member", () => {
    // Catches the lookup written as an inline `switch` beside an exported map
    // nobody reads: the map is then decoration, the two drift the first time
    // one is edited, and the case above passes for both because each answers
    // SOME non-empty label. This is the only case that fails when they
    // disagree, and it pins no text of its own, so it stays true whatever the
    // four labels end up saying.
    for (const kind of KINDS) {
      expect(notificationKindLabel(kind)).toBe(NOTIFICATION_KIND_LABELS[kind]);
    }
  });
});

describe("a kind no map knows", () => {
  it("renders the raw wire string", () => {
    // The raw-string case, and the same choice `review-labels.ts` makes one module over.
    // Catches the lookup written as a bare index —
    // `NOTIFICATION_KIND_LABELS[kind]` on a member Python added since renders
    // the literal "undefined" in front of a person — and the one written with a
    // vague default ("Notification"), which hides the drift instead of showing
    // a value someone can grep for against the enum the moment it is reported.
    expect(notificationKindLabel("not_a_member")).toBe("not_a_member");
  });
});
