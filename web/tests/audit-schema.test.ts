import { describe, expect, it } from "vitest";

import { auditSearchSchema, auditVerifySchema } from "@/lib/audit-schema";

/**
 * An earlier change §6, `audit-schema.test.ts`: the parser 24b's `/admin/audit` page
 * reads, over the two payloads the new proxies return.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 24a's Deviations):
 * - `web/lib/audit-schema.ts` exports `auditEventRowSchema`,
 *   `auditSearchSchema` and `auditVerifySchema`, plus the inferred types
 *   `AuditEventRow`, `AuditSearch` and `AuditVerify` — decision E: one file,
 *   both envelopes, `reviews-schema.ts`' own naming.
 * - the search envelope is `{ events: [...], next_before_seq: number | null }`
 *   with the NINE `AuditEventRow` fields, and the verify envelope
 *   `{ ok: boolean, broken_at: number | null }` — both read off
 *   `mcp_server/tools/read.py` and an earlier change's verify route, never invented
 *   here.
 * - every field is REQUIRED, and nullable exactly where the tool's own model
 *   is (`actor_ref`, `subject_id`, `next_before_seq`, `broken_at`), never
 *   `.optional()`: a missing column would otherwise reach the page as
 *   `undefined` and render as a blank cell instead of a refused payload.
 * - `payload` is an arbitrary JSON OBJECT (13b decision E, §5's third risk):
 *   a schema demanding a shape would reject real rows, because every event
 *   type writes its own keys.
 * - no schema is `.strict()`: an unknown extra key is accepted, because the
 *   wire may grow and a page that refuses a widened payload breaks on a
 *   deploy that changed nothing it reads.
 *
 * No backing service, no route, no page: this is the parser alone.
 */

const REQUEST_ID = "55555555-5555-5555-8555-555555555555";

/** One event row, every field present, obviously fake. */
const EVENT_ROW: Record<string, unknown> = {
  seq: 901,
  occurred_at: "2026-01-15T09:30:00Z",
  actor_kind: "user",
  actor_ref: "fake.person@example.invalid",
  event_type: "access_request.created",
  subject_type: "access_request",
  subject_id: REQUEST_ID,
  payload: { risk_class: "medium", role_name: "Fake Payroll Read" },
  hash: "0".repeat(64),
};

/** The nine field names of §1, written out rather than derived from the row. */
const EVENT_FIELDS = [
  "seq",
  "occurred_at",
  "actor_kind",
  "actor_ref",
  "event_type",
  "subject_type",
  "subject_id",
  "payload",
  "hash",
] as const;

function without(row: Record<string, unknown>, field: string): Record<string, unknown> {
  const copy = { ...row };
  delete copy[field];
  return copy;
}

function page(row: Record<string, unknown>, nextBeforeSeq: number | null = 901): unknown {
  return { events: [row], next_before_seq: nextBeforeSeq };
}

describe("auditSearchSchema", () => {
  it("accepts the whole nine-field row", () => {
    // The premise every rejection row below depends on: a schema that refused
    // the complete payload would make all nine of them pass while the page
    // rendered nothing at all. The count is asserted beside it because §1
    // fixes it at nine.
    expect(auditSearchSchema.safeParse(page(EVENT_ROW)).success).toBe(true);
    expect(EVENT_FIELDS).toHaveLength(9);
  });

  it.each(EVENT_FIELDS)("rejects an event row with no %s", (field) => {
    // Catches a parser that declares the field `.optional()` (or omits it
    // altogether): the page then renders `undefined` for a column the tool
    // promised, and on an AUDIT surface that is the worst kind of blank — an
    // event with no actor, no subject or no hash reads as a row nobody did
    // and nothing can be checked against, rather than as a refused payload.
    // `seq` is the same failure with paging attached: without it the cursor
    // the next page is asked for cannot be computed at all.
    expect(auditSearchSchema.safeParse(page(without(EVENT_ROW, field))).success).toBe(false);
  });

  it("accepts a null next_before_seq and a null actor_ref and subject_id", () => {
    // §1's "accepts `next_before_seq: null`" plus the two nullable columns of
    // the row itself. Catches a parser pinning any of the three to a
    // non-null type: the LAST page of the log answers `next_before_seq: null`,
    // so a page that refused it would show the auditor an error exactly when
    // they reached the end, and a system-written event (`actor_kind: "system"`)
    // carries no `actor_ref` at all while a tool-subject event carries no
    // `subject_id`.
    expect(auditSearchSchema.safeParse(page(EVENT_ROW, null)).success).toBe(true);
    expect(
      auditSearchSchema.safeParse(page({ ...EVENT_ROW, actor_ref: null, subject_id: null }))
        .success,
    ).toBe(true);
  });

  it("accepts an empty page and rejects a bare array", () => {
    // The envelope is the unit the page parses, so an empty result set is a
    // valid payload and a naked list is not. Catches a schema written over the
    // ROW rather than over the envelope — the page would then have to parse
    // per row and could never see `next_before_seq` at all.
    expect(auditSearchSchema.safeParse({ events: [], next_before_seq: null }).success).toBe(true);
    expect(auditSearchSchema.safeParse([EVENT_ROW]).success).toBe(false);
    expect(auditSearchSchema.safeParse({ events: [EVENT_ROW] }).success).toBe(false);
  });

  it("accepts a nested arbitrary payload whose keys no parser knows", () => {
    // §5's third risk. `payload` is an arbitrary JSON object by design (13b
    // decision E): every event type writes its own keys, and future ones will
    // write keys written after this file. Catches a parser that fixes a shape
    // for it — the page would refuse real rows from the log it exists to show,
    // and the refusal would look like tampering rather than like a schema.
    const nested = {
      ...EVENT_ROW,
      payload: {
        approvers: [{ slot: "department_manager", user_id: REQUEST_ID }],
        reasons: { policy: ["sod_conflict"], depth: { level: 3 } },
        never_seen_before: true,
      },
    };

    expect(auditSearchSchema.safeParse(page(nested)).success).toBe(true);
  });

  it("rejects a payload that is not an object", () => {
    // The other side of the same field: arbitrary is not untyped. Catches a
    // `z.unknown()` payload, which would let a string or a number through and
    // leave 24b's renderer walking entries of something that has none.
    expect(auditSearchSchema.safeParse(page({ ...EVENT_ROW, payload: "written" })).success).toBe(
      false,
    );
    expect(auditSearchSchema.safeParse(page({ ...EVENT_ROW, payload: null })).success).toBe(false);
  });

  it("accepts an event row and an envelope carrying an unknown extra key", () => {
    // Catches a `.strict()` schema. The wire may grow — 23e widened a tool's
    // output by six fields without touching any parser — and a page that
    // refused a payload carrying one new key would break on a deploy that
    // changed nothing it reads.
    expect(
      auditSearchSchema.safeParse(page({ ...EVENT_ROW, something_new: "later" })).success,
    ).toBe(true);
    expect(
      auditSearchSchema.safeParse({ ...(page(EVENT_ROW) as object), scanned: 500 }).success,
    ).toBe(true);
  });

  it("rejects a string seq and a string next_before_seq", () => {
    // Both are NUMBERS: the cursor is arithmetic the page does (the next
    // request asks for events before this `seq`), so a coerced string would
    // build `before_seq=901x` style queries or compare lexicographically and
    // silently skip a page of the log.
    expect(auditSearchSchema.safeParse(page({ ...EVENT_ROW, seq: "901" })).success).toBe(false);
    expect(
      auditSearchSchema.safeParse({ events: [EVENT_ROW], next_before_seq: "901" }).success,
    ).toBe(false);
  });

  it("exposes the seq, the hash and the payload on the parsed value", () => {
    // Catches fields parsed as `z.unknown()`: the seq is the cursor, the hash
    // is what an auditor checks against the verify answer, and the payload is
    // what the row actually says. Under strict TypeScript this case does not
    // compile if any of the three is unreadable.
    const parsed = auditSearchSchema.safeParse(page(EVENT_ROW));
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    expect(parsed.data.events[0]?.seq).toBe(901);
    expect(parsed.data.events[0]?.hash).toBe("0".repeat(64));
    expect(parsed.data.events[0]?.payload.risk_class).toBe("medium");
    expect(parsed.data.next_before_seq).toBe(901);
  });
});

describe("auditVerifySchema", () => {
  it("accepts both answers plan 07's route can give", () => {
    // §1's last criterion. An intact chain answers `broken_at: null` and a
    // tampered one names the sequence number it broke at. Catches a parser
    // that pins `broken_at` to a number — the ordinary, healthy answer would
    // then be refused and the page would report the chain unreadable on every
    // load — and one that pins it to a string, which would refuse the only
    // answer that matters.
    expect(auditVerifySchema.safeParse({ ok: true, broken_at: null }).success).toBe(true);
    expect(auditVerifySchema.safeParse({ ok: false, broken_at: 42 }).success).toBe(true);
  });

  it("rejects an answer with no ok", () => {
    // Catches an `.optional()` `ok`: the page reads it as `undefined`, which
    // is falsy, and would report a healthy chain as broken — or, with the
    // sense inverted, a broken one as healthy. There is no safe default for
    // this field, which is why it is required rather than defaulted.
    expect(auditVerifySchema.safeParse({ broken_at: null }).success).toBe(false);
  });

  it("rejects a non-boolean ok and accepts an unknown extra key", () => {
    // `ok` is a BOOLEAN: `"false"` is truthy in the page's own conditional, so
    // a coerced string would render a tampered chain as intact. The extra key
    // is the `.strict()` catch, for the same reason as the search envelope.
    expect(auditVerifySchema.safeParse({ ok: "true", broken_at: null }).success).toBe(false);
    expect(
      auditVerifySchema.safeParse({ ok: true, broken_at: null, checked_at: "2026-01-15" }).success,
    ).toBe(true);
  });

  it("exposes ok and broken_at on the parsed value", () => {
    // Catches fields parsed as `z.unknown()`: these two ARE the whole answer,
    // and a page that cannot read them has nothing to render.
    const parsed = auditVerifySchema.safeParse({ ok: false, broken_at: 42 });
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    expect(parsed.data.ok).toBe(false);
    expect(parsed.data.broken_at).toBe(42);
  });
});
