import { describe, expect, it } from "vitest";

import { reviewDecisionSchema, reviewItemsSchema, reviewQueueSchema } from "@/lib/reviews-schema";

/**
 * An earlier change §6, `reviews-schema.test.ts`: the parser 23f's `/reviews` page
 * reads, over the three payloads the new proxies return.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23h's Deviations):
 * - `web/lib/reviews-schema.ts` exports `reviewQueueSchema`,
 *   `reviewItemsSchema` and `reviewDecisionSchema`, one per envelope
 *   (decision E: one file, three envelopes), each written over the WHOLE
 *   envelope rather than over a bare row, so the page runs ONE `safeParse` on
 *   what the proxy returned — `approvals-schema.ts`' own shape.
 * - the queue envelope is `{ reviews: [...] }` with the THIRTEEN
 *   `ReviewQueueRow` fields, the item envelope `{ review_id, items: [...] }`
 *   with the ELEVEN `ReviewItemOutput` fields, and the decision envelope the
 *   six `DecideReviewItemOutput` fields — all read off
 *   `mcp_server/tools/reviews.py`, never invented here.
 * - every field is REQUIRED, and nullable exactly where the tool's own model
 *   is (`due_at`, `completed_at`, both scope id/name pairs, an undecided
 *   item's `decision`/`decided_at`, an open-ended grant's `expires_at`),
 *   never `.optional()`: a missing column would otherwise reach the page as
 *   `undefined` and render as a blank cell instead of a refused payload.
 * - no schema is `.strict()`: an unknown extra key is accepted, because the
 *   wire may grow (23e widened the item list by six fields without touching
 *   this file's predecessors) and a page that refuses a widened payload breaks
 *   on a deploy that changed nothing it reads.
 *
 * No backing service, no route, no page: this is the parser alone.
 */

const REVIEW_ID = "55555555-5555-5555-8555-555555555555";
const ITEM_ID = "66666666-6666-5666-8666-666666666666";

/** One queue row, every field present, obviously fake. */
const QUEUE_ROW: Record<string, unknown> = {
  review_id: REVIEW_ID,
  period: "2026-Q2",
  status: "open",
  due_at: "2026-06-30T23:59:00Z",
  completed_at: null,
  department_id: "33333333-3333-5333-8333-333333333333",
  department_name: "Fake Department",
  system_id: null,
  system_name: null,
  reviewer_user_id: "22222222-2222-5222-8222-222222222222",
  reviewer_display_name: "Fake Person One",
  item_count: 4,
  decided_count: 1,
};

/** The thirteen field names of §1, written out rather than derived from the row. */
const QUEUE_FIELDS = [
  "review_id",
  "period",
  "status",
  "due_at",
  "completed_at",
  "department_id",
  "department_name",
  "system_id",
  "system_name",
  "reviewer_user_id",
  "reviewer_display_name",
  "item_count",
  "decided_count",
] as const;

/** One item as 23e widened it, every field present, obviously fake. */
const ITEM_ROW: Record<string, unknown> = {
  item_id: ITEM_ID,
  grant_id: "77777777-7777-5777-8777-777777777777",
  flag: "non_standard",
  decision: null,
  decided_at: null,
  target_display_name: "Fake Person Two",
  role_name: "Fake Payroll Read",
  system_name: "Fake Payroll",
  privilege: "read",
  granted_at: "2026-01-04T09:00:00Z",
  expires_at: "2026-12-31T09:00:00Z",
};

/** The eleven field names of §1. */
const ITEM_FIELDS = [
  "item_id",
  "grant_id",
  "flag",
  "decision",
  "decided_at",
  "target_display_name",
  "role_name",
  "system_name",
  "privilege",
  "granted_at",
  "expires_at",
] as const;

/** `decide_review_item`'s answer, every field present. */
const DECISION: Record<string, unknown> = {
  item_id: ITEM_ID,
  review_id: REVIEW_ID,
  decision: "revoke",
  decided_at: "2026-06-02T10:15:00Z",
  revoked_grant_count: 1,
  review_complete: false,
};

const DECISION_FIELDS = [
  "item_id",
  "review_id",
  "decision",
  "decided_at",
  "revoked_grant_count",
  "review_complete",
] as const;

function without(row: Record<string, unknown>, field: string): Record<string, unknown> {
  const copy = { ...row };
  delete copy[field];
  return copy;
}

describe("reviewQueueSchema", () => {
  it("accepts the whole thirteen-field row", () => {
    // The premise every rejection row below depends on: a schema that refused
    // the complete payload would make all thirteen of them pass while the
    // queue rendered nothing at all. The count is asserted beside it because
    // §1 fixes it at thirteen.
    expect(reviewQueueSchema.safeParse({ reviews: [QUEUE_ROW] }).success).toBe(true);
    expect(QUEUE_FIELDS).toHaveLength(13);
  });

  it.each(QUEUE_FIELDS)("rejects a queue row with no %s", (field) => {
    // Catches a parser that declares the field `.optional()` (or omits it
    // altogether): the page then renders `undefined` for a column the tool
    // promised — a review with no period, no due date or no progress counts —
    // and the failure surfaces as a blank cell rather than as a refused
    // payload. One row per field, because a single missing-field case pins
    // only the field it happened to pick.
    expect(reviewQueueSchema.safeParse({ reviews: [without(QUEUE_ROW, field)] }).success).toBe(
      false,
    );
  });

  it("accepts a system-scoped row, where the department pair is null", () => {
    // `access_reviews` allows exactly ONE scope id, so on every row one of the
    // two id/name pairs is null. Catches `department_id: z.string()` (and its
    // three siblings), under which the parser would refuse every correctly
    // answered system-scoped review and the page would show an empty queue to
    // a system owner.
    const systemScoped = {
      ...QUEUE_ROW,
      department_id: null,
      department_name: null,
      system_id: "88888888-8888-5888-8888-888888888888",
      system_name: "Fake Payroll",
    };

    expect(reviewQueueSchema.safeParse({ reviews: [systemScoped] }).success).toBe(true);
  });

  it("accepts a completed row carrying a completed_at and rejects a string item_count", () => {
    // Two kills. `completed_at` is null while a review is open and a timestamp
    // once it closes, so a parser that pinned it to `null` would refuse the
    // history 23e's status filter exists to show. And the two counts are
    // NUMBERS: a `z.unknown()` or a coerced string would let the page compute
    // "1 of 4 decided" out of values it cannot subtract.
    const completed = { ...QUEUE_ROW, status: "completed", completed_at: "2026-07-01T09:00:00Z" };

    expect(reviewQueueSchema.safeParse({ reviews: [completed] }).success).toBe(true);
    expect(
      reviewQueueSchema.safeParse({ reviews: [{ ...QUEUE_ROW, item_count: "4" }] }).success,
    ).toBe(false);
  });

  it("accepts a queue row carrying an unknown extra field", () => {
    // Catches a `.strict()` schema: the wire may grow, and a page that refuses
    // a payload with one new key breaks on a deploy that changed nothing it
    // reads.
    expect(
      reviewQueueSchema.safeParse({ reviews: [{ ...QUEUE_ROW, something_new: "a later field" }] })
        .success,
    ).toBe(true);
  });

  it("accepts an empty queue and rejects a body with no reviews key", () => {
    // Catches a schema written over the ROW alone: the page parses what the
    // proxy returned, which is the envelope, and "no review is waiting on you"
    // is a valid answer that must not read as a parse failure — the tool
    // answers an empty list rather than NOT_FOUND precisely so the page can
    // say so.
    expect(reviewQueueSchema.safeParse({ reviews: [] }).success).toBe(true);
    expect(reviewQueueSchema.safeParse([QUEUE_ROW]).success).toBe(false);
    expect(reviewQueueSchema.safeParse({}).success).toBe(false);
  });

  it("exposes the review id and the two progress counts on the parsed value", () => {
    // Catches fields parsed as `z.unknown()`: the queue's whole job is linking
    // each review to its items and showing how far along it is, and a parsed
    // value with nothing to read leaves the page with a row it cannot open.
    // Under TypeScript's strict mode this case does not compile at all, which
    // is the earliest it can fail.
    const parsed = reviewQueueSchema.safeParse({ reviews: [QUEUE_ROW] });
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    expect(parsed.data.reviews[0]?.review_id).toBe(REVIEW_ID);
    expect(parsed.data.reviews[0]?.item_count).toBe(4);
    expect(parsed.data.reviews[0]?.decided_count).toBe(1);
  });
});

describe("reviewItemsSchema", () => {
  it("accepts the whole eleven-field item", () => {
    // The premise the eleven rejection rows depend on, and §1's count pinned
    // beside it.
    expect(reviewItemsSchema.safeParse({ review_id: REVIEW_ID, items: [ITEM_ROW] }).success).toBe(
      true,
    );
    expect(ITEM_FIELDS).toHaveLength(11);
  });

  it.each(ITEM_FIELDS)("rejects an item with no %s", (field) => {
    // Catches a parser that declares the field `.optional()`. The six context
    // fields 23e added are exactly the ones an implementer copying an older
    // parser leaves out, and without them a reviewer is asked to keep or
    // revoke a `grant_id` — a decision nobody can make from what is on screen.
    expect(
      reviewItemsSchema.safeParse({ review_id: REVIEW_ID, items: [without(ITEM_ROW, field)] })
        .success,
    ).toBe(false);
  });

  it("accepts a decided item and an open-ended grant", () => {
    // `decision` and `decided_at` are null until a reviewer acts and a string
    // afterwards; `expires_at` is null for a grant with no end. Catches a
    // parser pinning any of the three to null, which would refuse every item
    // the moment it was decided — the page would go blank on the row the user
    // just clicked — and one pinning them to a string, which would refuse
    // every undecided item, i.e. the whole point of the list.
    const decided = {
      ...ITEM_ROW,
      decision: "keep",
      decided_at: "2026-06-02T10:15:00Z",
      expires_at: null,
    };

    expect(reviewItemsSchema.safeParse({ review_id: REVIEW_ID, items: [decided] }).success).toBe(
      true,
    );
  });

  it("accepts an item carrying an unknown extra field", () => {
    // Catches a `.strict()` element schema, on the very tool this row's
    // predecessor widened by six fields.
    expect(
      reviewItemsSchema.safeParse({
        review_id: REVIEW_ID,
        items: [{ ...ITEM_ROW, something_new: "a later field" }],
      }).success,
    ).toBe(true);
  });

  it("accepts an empty item list and rejects a body with no review_id", () => {
    // Catches a schema written over the items ARRAY alone: the envelope names
    // the review the items belong to, and a page that lost it could not send a
    // decision back. An empty list is a valid answer — a review whose scope
    // held no live grants — and must not read as a parse failure.
    expect(reviewItemsSchema.safeParse({ review_id: REVIEW_ID, items: [] }).success).toBe(true);
    expect(reviewItemsSchema.safeParse({ items: [ITEM_ROW] }).success).toBe(false);
    expect(reviewItemsSchema.safeParse([ITEM_ROW]).success).toBe(false);
  });

  it("exposes the item id and the grant's names on the parsed value", () => {
    // Catches fields parsed as `z.unknown()`: the decision POST is addressed
    // by `item_id`, and the three names are what the reviewer actually reads
    // before deciding. Under strict TypeScript this case does not compile if
    // any of them is unreadable.
    const parsed = reviewItemsSchema.safeParse({ review_id: REVIEW_ID, items: [ITEM_ROW] });
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    expect(parsed.data.review_id).toBe(REVIEW_ID);
    expect(parsed.data.items[0]?.item_id).toBe(ITEM_ID);
    expect(parsed.data.items[0]?.target_display_name).toBe("Fake Person Two");
    expect(parsed.data.items[0]?.system_name).toBe("Fake Payroll");
  });
});

describe("reviewDecisionSchema", () => {
  it("accepts the whole decision answer", () => {
    // The premise the six rejection rows depend on.
    expect(reviewDecisionSchema.safeParse(DECISION).success).toBe(true);
    expect(DECISION_FIELDS).toHaveLength(6);
  });

  it.each(DECISION_FIELDS)("rejects a decision answer with no %s", (field) => {
    // Catches an `.optional()` field on the answer the page renders after a
    // click. `revoked_grant_count` and `review_complete` are the two that
    // matter most: the first is what tells a reviewer whether their Revoke
    // actually ended a grant or found it already gone, and the second is the
    // ONLY record that a review closed — there is no `review.completed` event
    // and no `complete_review` tool — so a page reading `undefined` for either
    // would report a revocation that did nothing as a success and leave a
    // finished review looking open.
    expect(reviewDecisionSchema.safeParse(without(DECISION, field)).success).toBe(false);
  });

  it("accepts a null decided_at and rejects a string revoked_grant_count", () => {
    // `decided_at` is `datetime | None` on the tool's own output, so a parser
    // pinning it to a string would refuse an answer the tool can legitimately
    // give. The count is a NUMBER: a coerced string would let the page render
    // "0" as truthy and tell a reviewer a grant was ended when none was.
    expect(reviewDecisionSchema.safeParse({ ...DECISION, decided_at: null }).success).toBe(true);
    expect(reviewDecisionSchema.safeParse({ ...DECISION, revoked_grant_count: "1" }).success).toBe(
      false,
    );
  });

  it("accepts a decision answer carrying an unknown extra field", () => {
    // Catches a `.strict()` schema, for the same reason as the two envelopes
    // above.
    expect(reviewDecisionSchema.safeParse({ ...DECISION, something_new: "later" }).success).toBe(
      true,
    );
  });
});
