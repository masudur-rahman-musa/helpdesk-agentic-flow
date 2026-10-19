import { describe, expect, it } from "vitest";

import { myAccessSchema, myRequestsSchema } from "@/features/access/schema";

/**
 * The parsers the access pages read, over the
 * two payloads `/api/me/requests` and `/api/me/access` return.
 *
 * Shapes this file fixes:
 * - `web/src/features/access/schema.ts` exports `myRequestsSchema` (the whole
 *   `{ requests: [...] }` envelope) and `myAccessSchema` (`{ grants: [...] }`),
 *   Zod objects over the WIRE bodies rather than bare row schemas, so a page
 *   runs ONE `safeParse` on what the proxy returned.
 * - every one of the nineteen fields is REQUIRED and nullable where the tool
 *   can answer null (`requested_expiry_days`, `grant_id`, `expires_at`), never
 *   `.optional()`: a payload missing one of the nineteen is rejected,
 *   and an optional field would be silently absent on every row instead.
 * - the schemas are NOT `.strict()`: an unknown extra key is accepted, because
 *   the wire may grow and a page that refuses a widened payload breaks on a
 *   deploy that changed nothing it reads.
 * - `approvals` entries expose at least `slot` and `user_id` as their own
 *   fields (the page renders the chain by slot), which is what the last case
 *   reads off the parsed value rather than off the input.
 *
 * No backing service, no route, no page: this is the parser alone.
 */

/** One widened row, every field present, obviously fake. */
const REQUEST_ROW: Record<string, unknown> = {
  request_id: "66666666-6666-5666-8666-666666666666",
  request_number: "REQ-0107",
  target_user_id: "22222222-2222-5222-8222-222222222222",
  role_id: "77777777-7777-5777-8777-777777777777",
  justification: "Obviously fake justification.",
  requested_expiry_days: 30,
  risk_class: "elevated",
  decision: "approval",
  status: "pending",
  created_at: "2026-06-02T10:15:00Z",
  role_slug: "payroll-read",
  role_name: "Fake Payroll Read",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  target_display_name: "Fake Person One",
  decision_reasons: ["NOT_IN_TEMPLATE"],
  citations: [],
  approvals: [{ slot: "department_manager", user_id: null }],
  grant_id: null,
  expires_at: null,
};

const GRANT_ROW: Record<string, unknown> = {
  grant_id: "55555555-5555-5555-8555-555555555555",
  role_slug: "payroll-read",
  role_name: "Fake Payroll Read",
  privilege: "read",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  granted_at: "2026-01-05T09:00:00Z",
  expires_at: "2099-03-01T12:00:00Z",
};

/** The twenty field names, written out rather than derived from the row. */
const REQUEST_FIELDS = [
  "request_id",
  "request_number",
  "target_user_id",
  "role_id",
  "justification",
  "requested_expiry_days",
  "risk_class",
  "decision",
  "status",
  "created_at",
  "role_slug",
  "role_name",
  "system_slug",
  "system_name",
  "target_display_name",
  "decision_reasons",
  "citations",
  "approvals",
  "grant_id",
  "expires_at",
] as const;

function withoutField(field: string): Record<string, unknown> {
  const row = { ...REQUEST_ROW };
  delete row[field];
  return row;
}

describe("myRequestsSchema", () => {
  it("accepts the whole twenty-field row", () => {
    // The premise every rejection row below depends on: a schema that refused
    // the complete payload would make all nineteen of them pass while the page
    // rendered nothing. Read off the same constant they are built from.
    expect(myRequestsSchema.safeParse({ requests: [REQUEST_ROW] }).success).toBe(true);
    expect(REQUEST_FIELDS).toHaveLength(20);
  });

  it.each(REQUEST_FIELDS)("rejects a row with no %s", (field) => {
    // Catches a parser that declares the field `.optional()` (or omits it
    // altogether): the page then renders `undefined` for a column the tool
    // promised, and the failure surfaces as a blank cell rather than as a
    // refused payload. One row per field, because a single missing-field case
    // pins only the field it happened to pick — and `grant_id`/`expires_at`,
    // which are legitimately NULL, are exactly the two an implementer is most
    // likely to make optional instead.
    expect(myRequestsSchema.safeParse({ requests: [withoutField(field)] }).success).toBe(false);
  });

  it("accepts a row carrying an unknown extra field", () => {
    // Catches a `.strict()` schema: the wire may grow (the schema is itself a
    // widening of the same tool), and a page that refuses a payload with one
    // new key breaks on a deploy that changed nothing it reads.
    const widened = { ...REQUEST_ROW, something_new: "a field 23c adds" };

    expect(myRequestsSchema.safeParse({ requests: [widened] }).success).toBe(true);
  });

  it("accepts an empty list and rejects a body with no requests key", () => {
    // Catches a schema written over the ROW alone: the page parses what the
    // proxy returned, which is the envelope, and "you have filed nothing" is a
    // valid answer that must not read as a parse failure.
    expect(myRequestsSchema.safeParse({ requests: [] }).success).toBe(true);
    expect(myRequestsSchema.safeParse([REQUEST_ROW]).success).toBe(false);
    expect(myRequestsSchema.safeParse({}).success).toBe(false);
  });

  it("exposes each approval seat's slot on the parsed value", () => {
    // Catches `approvals` typed as an opaque array (`z.array(z.unknown())`):
    // the page renders the chain BY SLOT, so a parsed value with no `slot`
    // to read leaves the page with a row it cannot draw — and under
    // TypeScript's strict mode this case does not compile at all, which is the
    // earliest the failure can be caught.
    const parsed = myRequestsSchema.safeParse({ requests: [REQUEST_ROW] });
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    expect(parsed.data.requests[0]?.approvals[0]?.slot).toBe("department_manager");
  });
});

describe("myAccessSchema", () => {
  it("accepts the grants envelope and an empty list", () => {
    expect(myAccessSchema.safeParse({ grants: [GRANT_ROW] }).success).toBe(true);
    expect(myAccessSchema.safeParse({ grants: [] }).success).toBe(true);
  });

  it("rejects a grant with no role_name and accepts one with an extra field", () => {
    // The same two kills as above, on the second payload: an optional
    // `role_name` renders a grant the user cannot identify, and a `.strict()`
    // schema refuses a widened wire. `role_name` rather than `grant_id`
    // deliberately — it is a field the page DISPLAYS, so a parser that checked
    // only the ids would pass every other case in this file.
    const withoutRoleName = { ...GRANT_ROW };
    delete withoutRoleName.role_name;

    expect(myAccessSchema.safeParse({ grants: [withoutRoleName] }).success).toBe(false);
    expect(
      myAccessSchema.safeParse({ grants: [{ ...GRANT_ROW, something_new: "later" }] }).success,
    ).toBe(true);
  });
});

/**
 * `citations` as the typed five-field object rather than
 * `z.array(z.unknown())`. The existing request fixture's `citations: []` still parses.
 *
 * The element shape is `mcp_server.tools.access.Citation` as STORED:
 * `{document_slug, title, version, heading_path, chunk_id}`, five fields, with
 * `heading_path` a STRING. It is NOT the chat citation of
 * `graph/state.py::Citation`, which carries `text` and `score` the request row
 * has never held and a `heading_path` that is a list.
 */
describe("myRequestSchema — citations", () => {
  const CITATION: Record<string, unknown> = {
    document_slug: "fake-access-policy",
    title: "Fake Access Policy",
    version: "v3",
    heading_path: "Fake Section › Fake Subsection",
    chunk_id: "99999999-9999-5999-8999-999999999999",
  };

  function withCitation(citation: unknown): { requests: unknown[] } {
    return { requests: [{ ...REQUEST_ROW, citations: [citation] }] };
  }

  it("accepts the five-field citation", () => {
    // The premise the rejection rows below rest on: a schema refusing the
    // complete citation would make every one of them pass while `/requests`
    // rendered an alert for a payload that is exactly on contract.
    expect(myRequestsSchema.safeParse(withCitation(CITATION)).success).toBe(true);
  });

  const MISSING = ["document_slug", "title", "version", "heading_path", "chunk_id"] as const;

  it.each(MISSING)("rejects a citation with no %s", (field) => {
    // Catches the `z.array(z.unknown())` this replaces: every row here
    // passes under it, and `/requests` then renders a source with a blank
    // title or a version it cannot show — the two fields it must
    // name. `chunk_id` in particular is the field a hand-written citation
    // forgets, and it is what ties a source back to the text it came from.
    const citation = { ...CITATION };
    delete citation[field];

    expect(myRequestsSchema.safeParse(withCitation(citation)).success).toBe(false);
  });

  it("rejects a citation whose heading_path is a list", () => {
    // Pinned on the field the two citation shapes disagree about.
    // Catches a schema copied from the CHAT citation (`heading_path:
    // z.array(z.string())`, plus `text` and `score`): it would refuse every
    // stored request citation on the wire, so the page would alert on a row the
    // tool answered correctly. Nothing else in this file distinguishes the two
    // shapes.
    expect(
      myRequestsSchema.safeParse(withCitation({ ...CITATION, heading_path: ["Fake Section"] }))
        .success,
    ).toBe(false);
  });

  it("accepts a citation carrying an unknown sixth key", () => {
    // Catches a `.strict()` element schema. The Python model forbids extras
    // TODAY, but rows already stored are JSONB read back as they were written,
    // and a page that refuses a widened citation breaks on a deploy that
    // changed nothing it reads — the same reason the row schema is not strict.
    expect(
      myRequestsSchema.safeParse(withCitation({ ...CITATION, relevance: "later" })).success,
    ).toBe(true);
  });
});
