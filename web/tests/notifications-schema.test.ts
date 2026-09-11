import { describe, expect, it } from "vitest";

import {
  notificationPageSchema,
  notificationReadSchema,
  notificationRowSchema,
} from "@/lib/notifications-schema";

/**
 * An earlier change section 6, `notifications-schema.test.ts`: the parser 23gc's
 * `/notifications` page and its bell read, over the two payloads the new
 * proxies return.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23gb's Deviations):
 * - `web/lib/notifications-schema.ts` exports `notificationRowSchema`,
 *   `notificationPageSchema` and `notificationReadSchema` (decision E: one
 *   file, both envelopes). The page and the stamp schemas are written over the
 *   WHOLE envelope, so 23gc runs ONE `safeParse` on what the proxy returned --
 *   `approvals-schema.ts`' and `reviews-schema.ts`' own shape -- and the row
 *   schema is exported because section 6 names it and because the page schema
 *   has to be built out of it rather than out of a looser inline copy.
 * - the page envelope is `{ notifications: [...], unread_count: number }` with
 *   the EIGHT `NotificationRow` fields, and the stamp envelope the FOUR
 *   `MarkNotificationReadOutput` fields -- both read off
 *   `mcp_server/tools/notifications.py`, never invented here.
 * - every field is REQUIRED, and nullable exactly where the tool's own model
 *   is: `subject_type`, `subject_id` and a row's `read_at`. Never
 *   `.optional()`: a missing column would otherwise reach the page as
 *   `undefined` and render as a blank cell instead of a refused payload, which
 *   is why a nullable field and a missing one are two different wire answers
 *   and get two different rows below.
 * - the stamp answer's `read_at` is NOT nullable: the tool declares it non-null
 *   by construction, because the row it answers was stamped either by this call
 *   or by an earlier one.
 * - no schema is `.strict()`: an unknown extra key is accepted, because the
 *   wire may grow and a page that refuses a widened payload breaks on a deploy
 *   that changed nothing it reads.
 *
 * No backing service, no route, no page: this is the parser alone.
 */

const NOTIFICATION_ID = "99999999-9999-5999-8999-999999999999";
const REQUEST_ID = "77777777-7777-5777-8777-777777777777";

/** One notification row, every field present, obviously fake. */
const ROW: Record<string, unknown> = {
  notification_id: NOTIFICATION_ID,
  kind: "request_approved",
  title: "Your access request was approved",
  body: "Fake Payroll Read is yours until the end of the quarter.",
  subject_type: "access_request",
  subject_id: REQUEST_ID,
  created_at: "2026-06-02T10:15:00Z",
  read_at: null,
};

/** The eight field names of section 1, written out rather than derived from the row. */
const ROW_FIELDS = [
  "notification_id",
  "kind",
  "title",
  "body",
  "subject_type",
  "subject_id",
  "created_at",
  "read_at",
] as const;

/** The three the tool declares nullable, which is not the same as absent. */
const NULLABLE_ROW_FIELDS = ["subject_type", "subject_id", "read_at"] as const;

/** `mark_notification_read`'s answer, every field present. */
const STAMP: Record<string, unknown> = {
  notification_id: NOTIFICATION_ID,
  kind: "request_approved",
  title: "Your access request was approved",
  read_at: "2026-06-02T11:00:00Z",
};

const STAMP_FIELDS = ["notification_id", "kind", "title", "read_at"] as const;

function without(row: Record<string, unknown>, field: string): Record<string, unknown> {
  const copy = { ...row };
  delete copy[field];
  return copy;
}

function page(rows: Record<string, unknown>[], unreadCount: unknown = 0): Record<string, unknown> {
  return { notifications: rows, unread_count: unreadCount };
}

describe("notificationRowSchema and notificationPageSchema", () => {
  it("accepts the whole eight-field row inside the two-key envelope", () => {
    // The premise every rejection row below depends on: a schema that refused
    // the complete payload would make all eight of them pass while the page
    // rendered nothing at all. The count is asserted beside it because
    // section 1 fixes it at eight.
    expect(notificationRowSchema.safeParse(ROW).success).toBe(true);
    expect(notificationPageSchema.safeParse(page([ROW], 3)).success).toBe(true);
    expect(ROW_FIELDS).toHaveLength(8);
  });

  it.each(ROW_FIELDS)("rejects a notification row with no %s", (field) => {
    // Catches a parser that declares the field `.optional()` (or omits it
    // altogether): the page then renders `undefined` for a column the tool
    // promised -- a message with no title, no body or no arrival time, and for
    // `read_at` a bell that cannot tell an opened message from a new one -- and
    // the failure surfaces as a blank cell rather than as a refused payload.
    // One row per field, because a single missing-field case pins only the
    // field it happened to pick.
    //
    // Both schemas are asserted, which is what catches a page envelope built
    // over a looser inline element instead of over `notificationRowSchema`:
    // the row schema alone would stay strict while the payload the page
    // actually parses went through untouched.
    expect(notificationRowSchema.safeParse(without(ROW, field)).success).toBe(false);
    expect(notificationPageSchema.safeParse(page([without(ROW, field)])).success).toBe(false);
  });

  it.each(NULLABLE_ROW_FIELDS)("accepts a row whose %s is null", (field) => {
    // A nullable field and a missing one are two different wire answers
    // (section 6). `subject_type` and `subject_id` are null on a notification
    // about nothing in particular, and `read_at` is null on exactly the rows
    // the bell counts. Catches `z.string()` on any of the three, under which
    // the parser would refuse every unread message -- the whole point of the
    // page -- while the missing-field rows above still passed.
    expect(notificationRowSchema.safeParse({ ...ROW, [field]: null }).success).toBe(true);
    expect(notificationPageSchema.safeParse(page([{ ...ROW, [field]: null }])).success).toBe(true);
  });

  it("accepts a read row carrying a read_at timestamp", () => {
    // The other side of the nullable `read_at`: a row is stamped once the
    // person opens it, and an absent `unread_only` filter means the page lists
    // read and unread alike (section 1). Catches a parser pinning `read_at` to
    // `null`, which would refuse every message the person has already seen and
    // leave the default page empty for anyone who reads their bell.
    expect(
      notificationPageSchema.safeParse(page([{ ...ROW, read_at: "2026-06-02T11:00:00Z" }])).success,
    ).toBe(true);
  });

  it("rejects an envelope with no unread_count and one whose unread_count is a string", () => {
    // Section 1's two-key envelope, and 23ga decision B's reason for it: the
    // bell is a count of EVERY unread row and not the page's own tally.
    // Catches a parser written over `{ notifications }` alone -- the bell would
    // read `undefined` and render nothing, so a person with unread messages
    // sees no badge at all -- and one that accepts a coerced string, which the
    // badge would happily render while `unread_count > 0` compared a string
    // against a number.
    expect(notificationPageSchema.safeParse({ notifications: [ROW] }).success).toBe(false);
    expect(notificationPageSchema.safeParse(page([ROW], "3")).success).toBe(false);
  });

  it("accepts an empty page with unread_count 0 and rejects a bare array", () => {
    // Catches a schema written over the ROW or the LIST alone: the page parses
    // what the proxy returned, which is the envelope, and "nothing is waiting
    // for you" is a valid answer that must not read as a parse failure -- the
    // tool answers an empty list and a zero rather than NOT_FOUND precisely so
    // the page can say so.
    expect(notificationPageSchema.safeParse(page([], 0)).success).toBe(true);
    expect(notificationPageSchema.safeParse([ROW]).success).toBe(false);
    expect(notificationPageSchema.safeParse({}).success).toBe(false);
  });

  it("accepts a row and an envelope carrying an unknown extra field", () => {
    // Catches a `.strict()` schema at either level: the wire may grow, and a
    // page that refuses a payload with one new key breaks on a deploy that
    // changed nothing it reads.
    expect(notificationRowSchema.safeParse({ ...ROW, something_new: "later" }).success).toBe(true);
    expect(
      notificationPageSchema.safeParse({ ...page([ROW], 1), something_new: "later" }).success,
    ).toBe(true);
  });

  it("exposes the notification id, the subject and the unread count on the parsed value", () => {
    // Catches fields parsed as `z.unknown()`: the stamp POST is addressed by
    // `notification_id`, the subject pair is what lets a row link to the
    // request it is about, and the count is the bell itself -- a parsed value
    // with nothing to read leaves the page with a list it cannot act on. Under
    // TypeScript's strict mode this case does not compile at all, which is the
    // earliest it can fail.
    const parsed = notificationPageSchema.safeParse(page([ROW], 3));
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    expect(parsed.data.notifications[0]?.notification_id).toBe(NOTIFICATION_ID);
    expect(parsed.data.notifications[0]?.subject_id).toBe(REQUEST_ID);
    expect(parsed.data.notifications[0]?.read_at).toBeNull();
    expect(parsed.data.unread_count).toBe(3);
  });
});

describe("notificationReadSchema", () => {
  it("accepts the whole four-field stamp answer", () => {
    // The premise the four rejection rows depend on, and section 1's count
    // pinned beside it.
    expect(notificationReadSchema.safeParse(STAMP).success).toBe(true);
    expect(STAMP_FIELDS).toHaveLength(4);
  });

  it.each(STAMP_FIELDS)("rejects a stamp answer with no %s", (field) => {
    // Catches an `.optional()` field on the answer the page renders after a
    // click. `read_at` is the one that matters most: it is the ONLY record that
    // the click did anything, so a page reading `undefined` for it would leave
    // the row looking unread and the badge un-decremented while the row had in
    // fact been stamped. `notification_id` is how the page finds the row to
    // update at all.
    expect(notificationReadSchema.safeParse(without(STAMP, field)).success).toBe(false);
  });

  it("rejects a stamp answer whose read_at is null", () => {
    // The one place the stamp envelope is NARROWER than the row: the tool
    // declares `read_at` non-null by construction -- it either wrote the
    // timestamp or found the row already carrying one -- so a null here is a
    // far side that stopped keeping its own promise. Catches a parser that
    // copied the row's `.nullable()` across, under which the page would render
    // an opened message with no moment it was opened and nothing would be red.
    expect(notificationReadSchema.safeParse({ ...STAMP, read_at: null }).success).toBe(false);
  });

  it("accepts a stamp answer carrying an unknown extra field", () => {
    // Catches a `.strict()` schema, for the same reason as the page envelope
    // above.
    expect(notificationReadSchema.safeParse({ ...STAMP, something_new: "later" }).success).toBe(
      true,
    );
  });

  it("exposes the stamped row's id and read_at on the parsed value", () => {
    // Catches fields parsed as `z.unknown()`: after a click the page updates
    // exactly the row the answer names and shows it as read from that moment.
    // Under strict TypeScript this case does not compile if either is
    // unreadable.
    const parsed = notificationReadSchema.safeParse(STAMP);
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    expect(parsed.data.notification_id).toBe(NOTIFICATION_ID);
    expect(parsed.data.read_at).toBe("2026-06-02T11:00:00Z");
  });
});
