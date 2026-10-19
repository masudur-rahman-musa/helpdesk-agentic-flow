import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AuditEventTable } from "@/features/admin/audit/components/audit-event-table";
import { auditSearchSchema, type AuditEventRow } from "@/features/admin/audit/schema";

/**
 * `audit-numbers.test.tsx`: the Subject id cell of the audit table
 * shows a request's readable number in place of the stored id when the row
 * carries one, and the stored id (or "no subject id") otherwise.
 *
 * The unit is `AuditEventTable` rendered with fixture rows, no fetch and no
 * page. The row's Subject id cell is read from the rendered row, with the
 * payload disclosure removed first, because the payload shows whole and may
 * hold ids by design.
 */

const UUID_SHAPE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

const REQUEST_ID = "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa";
const GRANT_ID = "bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb";
const PAYLOAD_ID = "cccccccc-cccc-5ccc-8ccc-cccccccccccc";
const NUMBER = "REQ-0107";

const BASE: AuditEventRow = {
  seq: 700,
  occurred_at: "2026-09-12T08:15:30.123456Z",
  actor_kind: "user",
  actor_ref: "fake-person-one",
  event_type: "access_request.created",
  subject_type: "access_request",
  subject_id: REQUEST_ID,
  subject_number: NUMBER,
  // The payload holds a UUID-shaped string on purpose: the first case must
  // look for ids OUTSIDE it only.
  payload: { fake_ref: PAYLOAD_ID },
  hash: "ab".repeat(32),
};

/** The single data row's text, without the payload disclosure. */
function rowTextWithoutPayload(): string {
  const dataRow = screen
    .getAllByRole("row")
    .find((row) => within(row).queryAllByRole("cell").length > 0);
  if (dataRow === undefined) {
    throw new Error("expected one data row");
  }
  const copy = dataRow.cloneNode(true) as HTMLElement;
  copy.querySelectorAll("details").forEach((details) => details.remove());
  return copy.textContent ?? "";
}

describe("the Subject id cell of the audit table", () => {
  it("the subject cell shows REQ-0107 and no uuid for a numbered event", () => {
    // Catches a table that still prints the stored id (instead of, or beside,
    // the number): the person reading the audit page must see only the
    // readable number. The id is a real UUID shape, set on the same row, so
    // only the number-over-id choice can flip the result.
    render(<AuditEventTable events={[BASE]} />);

    const text = rowTextWithoutPayload();

    expect(text).toContain(NUMBER);
    expect(text).not.toMatch(UUID_SHAPE);
  });

  it("an event with no number still shows its stored subject id", () => {
    // Catches a table that renders only `subject_number` (blank or "null"
    // for a grant row) or hides the id when the number is absent: an event
    // whose subject is not a request must keep showing the id that pastes
    // into the Subject id filter.
    const grant: AuditEventRow = {
      ...BASE,
      event_type: "grant.created",
      subject_type: "grant",
      subject_id: GRANT_ID,
      subject_number: null,
    };
    render(<AuditEventTable events={[grant]} />);

    const text = rowTextWithoutPayload();

    expect(text).toContain(GRANT_ID);
    expect(text).not.toContain("REQ-");
  });

  it('an event with no subject shows "no subject id"', () => {
    // Catches a fallback chain that, with both fields null, prints "null",
    // an empty cell, or the number's placeholder instead of the fixed words.
    const system: AuditEventRow = {
      ...BASE,
      actor_kind: "system",
      actor_ref: null,
      event_type: "review.opened",
      subject_type: "access_review",
      subject_id: null,
      subject_number: null,
    };
    render(<AuditEventTable events={[system]} />);

    const text = rowTextWithoutPayload();

    expect(text).toContain("no subject id");
    expect(text).not.toContain("null");
  });
});

describe("auditSearchSchema and subject_number", () => {
  it("a row missing subject_number fails the parse", () => {
    // Catches a schema that leaves `subject_number` optional: a dropped key
    // would reach the table as `undefined` and show the raw id the page
    // exists to replace. The same row WITH the key (null) is parsed first, so
    // the refusal can only come from the missing key, not from a neighbour.
    const withoutNumber: Record<string, unknown> = { ...BASE };
    delete withoutNumber.subject_number;
    const withNull = { ...BASE, subject_number: null };

    expect(auditSearchSchema.safeParse({ events: [withNull], next_before_seq: null }).success).toBe(
      true,
    );
    expect(
      auditSearchSchema.safeParse({ events: [withoutNumber], next_before_seq: null }).success,
    ).toBe(false);
  });
});
