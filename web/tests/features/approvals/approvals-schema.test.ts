import { describe, expect, it } from "vitest";

import { pendingApprovalsSchema } from "@/features/approvals/schema";

/**
 * An earlier change §6, `approvals-schema.test.ts`: the parser 23d's `/approvals` page
 * reads, over the payload `/api/me/approvals` returns.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23c's Deviations):
 * - `web/src/features/approvals/schema.ts` exports `pendingApprovalsSchema`, a Zod
 *   object over the whole `{ approvals: [...] }` ENVELOPE rather than over a
 *   bare row, so the page runs ONE `safeParse` on what the proxy returned —
 *   the shape `access-schema.ts` already has for `/api/me/requests`.
 * - every one of the twenty fields is REQUIRED, and nullable only where the
 *   tool can answer null (`requested_expiry_days`, and an unresolved seat's
 *   `user_id`), never `.optional()`: §6 says a payload missing one of the
 *   twenty is rejected, and an optional field would be silently absent on
 *   every row and render as a blank cell instead.
 * - the schema is NOT `.strict()`: an unknown extra key is accepted, because
 *   the wire may grow and a page that refuses a widened payload breaks on a
 *   deploy that changed nothing it reads.
 * - `my_slot` and each `approvals` seat's `slot` are readable off the PARSED
 *   value, because the queue renders the chain by slot and names the seat the
 *   approver is being asked to fill.
 *
 * No backing service, no route, no page: this is the parser alone.
 */

/** One widened queue row, every field present, obviously fake. */
const APPROVAL_ROW: Record<string, unknown> = {
  request_id: "66666666-6666-5666-8666-666666666666",
  requester_user_id: "88888888-8888-5888-8888-888888888888",
  target_user_id: "99999999-9999-5999-8999-999999999999",
  role_id: "77777777-7777-5777-8777-777777777777",
  justification: "Obviously fake justification.",
  requested_expiry_days: 30,
  risk_class: "elevated",
  decision: "approval",
  status: "pending",
  created_at: "2026-06-02T10:15:00Z",
  my_slot: "department_manager",
  approvals: [
    { slot: "department_manager", user_id: "22222222-2222-5222-8222-222222222222" },
    { slot: "it_admin", user_id: null },
  ],
  role_slug: "payroll-read",
  role_name: "Fake Payroll Read",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  target_display_name: "Fake Person One",
  requester_display_name: "Fake Person Two",
  decision_reasons: ["NOT_IN_TEMPLATE", "APPROVAL_REQUIRED"],
  citations: [
    {
      document_slug: "fake-access-policy",
      title: "Fake Access Policy",
      version: "v2",
      heading_path: "Fake Section",
      chunk_id: "fake-chunk-0023",
    },
  ],
};

/** The twenty field names of §1, written out rather than derived from the row. */
const APPROVAL_FIELDS = [
  "request_id",
  "requester_user_id",
  "target_user_id",
  "role_id",
  "justification",
  "requested_expiry_days",
  "risk_class",
  "decision",
  "status",
  "created_at",
  "my_slot",
  "approvals",
  "role_slug",
  "role_name",
  "system_slug",
  "system_name",
  "target_display_name",
  "requester_display_name",
  "decision_reasons",
  "citations",
] as const;

function withoutField(field: string): Record<string, unknown> {
  const row = { ...APPROVAL_ROW };
  delete row[field];
  return row;
}

describe("pendingApprovalsSchema", () => {
  it("accepts the whole twenty-field row", () => {
    // The premise every rejection row below depends on: a schema that refused
    // the complete payload would make all twenty of them pass while the queue
    // rendered nothing at all. Read off the same constant they are built from.
    expect(pendingApprovalsSchema.safeParse({ approvals: [APPROVAL_ROW] }).success).toBe(true);
    expect(APPROVAL_FIELDS).toHaveLength(20);
  });

  it.each(APPROVAL_FIELDS)("rejects a row with no %s", (field) => {
    // Catches a parser that declares the field `.optional()` (or omits it
    // altogether): the page then renders `undefined` for a column the tool
    // promised, and the failure surfaces as a blank cell rather than as a
    // refused payload. One row per field, because a single missing-field case
    // pins only the field it happened to pick — and the eight fields 23c adds
    // are exactly the ones an implementer copying 23a's parser leaves out.
    expect(pendingApprovalsSchema.safeParse({ approvals: [withoutField(field)] }).success).toBe(
      false,
    );
  });

  it("accepts a row carrying an unknown extra field", () => {
    // Catches a `.strict()` schema: the wire may grow (this plan is itself a
    // widening of the same tool), and a page that refuses a payload with one
    // new key breaks on a deploy that changed nothing it reads.
    const widened = { ...APPROVAL_ROW, something_new: "a field 23e adds" };

    expect(pendingApprovalsSchema.safeParse({ approvals: [widened] }).success).toBe(true);
  });

  it("accepts an empty queue and rejects a body with no approvals key", () => {
    // Catches a schema written over the ROW alone: the page parses what the
    // proxy returned, which is the envelope, and "nothing is waiting for you"
    // is a valid answer that must not read as a parse failure.
    expect(pendingApprovalsSchema.safeParse({ approvals: [] }).success).toBe(true);
    expect(pendingApprovalsSchema.safeParse([APPROVAL_ROW]).success).toBe(false);
    expect(pendingApprovalsSchema.safeParse({}).success).toBe(false);
  });

  it("accepts a seat with a null user_id and rejects one with no slot", () => {
    // Two kills on the chain. A group seat (`hr`, `it_admin`) names nobody, so
    // a `user_id: z.string()` would refuse a payload the tool answers
    // correctly for every multi-seat chain; and an `approvals` typed as an
    // opaque array leaves the page with seats it cannot draw.
    const seatWithNoSlot = { ...APPROVAL_ROW, approvals: [{ user_id: null }] };

    expect(pendingApprovalsSchema.safeParse({ approvals: [APPROVAL_ROW] }).success).toBe(true);
    expect(pendingApprovalsSchema.safeParse({ approvals: [seatWithNoSlot] }).success).toBe(false);
  });

  it("exposes my_slot and each seat's slot on the parsed value", () => {
    // Catches `my_slot` parsed as `z.unknown()` and `approvals` as an opaque
    // array: the queue's whole point is telling an approver WHICH seat is
    // theirs, and a parsed value with nothing to read leaves the page showing
    // a request with no action attached to it. Under TypeScript's strict mode
    // this case does not compile at all, which is the earliest it can fail.
    const parsed = pendingApprovalsSchema.safeParse({ approvals: [APPROVAL_ROW] });
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    expect(parsed.data.approvals[0]?.my_slot).toBe("department_manager");
    expect(parsed.data.approvals[0]?.approvals[1]?.slot).toBe("it_admin");
    expect(parsed.data.approvals[0]?.approvals[1]?.user_id).toBeNull();
  });

  it("rejects a citation with no title and accepts one with a sixth key", () => {
    // The decision trace is what the design puts on the row, and `title` is what
    // the queue NAMES per source. Catches `citations: z.array(z.unknown())`,
    // under which a source with no title reaches the page as a blank line;
    // and catches a `.strict()` element, which would refuse a stored citation
    // that grew a key — JSONB rows are read back as they were written.
    const [citation] = APPROVAL_ROW.citations as Record<string, unknown>[];
    const withoutTitle = { ...citation };
    delete withoutTitle.title;

    expect(
      pendingApprovalsSchema.safeParse({
        approvals: [{ ...APPROVAL_ROW, citations: [withoutTitle] }],
      }).success,
    ).toBe(false);
    expect(
      pendingApprovalsSchema.safeParse({
        approvals: [{ ...APPROVAL_ROW, citations: [{ ...citation, relevance: "later" }] }],
      }).success,
    ).toBe(true);
  });
});
