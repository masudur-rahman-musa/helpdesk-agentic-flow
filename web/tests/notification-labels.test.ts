import { describe, expect, it } from "vitest";

import { NOTIFICATION_KIND_LABELS, notificationKindLabel } from "@/lib/notification-labels";

/**
 * An earlier change §6, `notification-labels.test.ts`: the hand-kept mirror of
 * `helpdesk.models.notification.NotificationKind`, as literals. Pure — no DOM,
 * no fetch, no React. (No basename collision in the tree, a project rule:
 * the neighbours are `notifications-schema.test.ts` and
 * `notifications-proxy-routes.test.ts`.)
 *
 * Every key asserted here is the enum's VALUE as `model_dump(mode="json")` puts
 * it on the wire (`grant_expired`), never the Python member name.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23gc's Deviations): §6 asks that "each of the four wire
 * values renders its sentence" but writes none of the four sentences down, so
 * no case here pins a TEXT — the strongest true statement available is "a
 * non-empty label that is not the wire code itself", plus the map/lookup
 * agreement below. `review-labels.test.ts` made exactly the same call one
 * module over, for the same reason: inventing the four sentences here would
 * leave the implementer free to invent a fifth answer this file happens not to
 * contradict.
 */

/** Every `helpdesk.models.notification.NotificationKind` member, as its wire value. */
const KINDS = ["grant_expired", "grant_expiring", "review_overdue", "review_escalated"] as const;

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

describe("the map and the lookup", () => {
  it("holds exactly the four NotificationKind members", () => {
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
    // §6's third bullet, and `review-labels.ts`' decision one module over.
    // Catches the lookup written as a bare index —
    // `NOTIFICATION_KIND_LABELS[kind]` on a member Python added since renders
    // the literal "undefined" in front of a person — and the one written with a
    // vague default ("Notification"), which hides the drift instead of showing
    // a value someone can grep for against the enum the moment it is reported.
    expect(notificationKindLabel("not_a_member")).toBe("not_a_member");
  });
});
