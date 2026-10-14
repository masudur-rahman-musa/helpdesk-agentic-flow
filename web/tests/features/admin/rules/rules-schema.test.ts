import { describe, expect, it } from "vitest";

import { sodRuleRoleBlockSchema, sodRuleRowSchema, sodRulesBoardSchema } from "@/features/admin/rules/schema";

/**
 * An earlier change §6, `rules-schema.test.ts`: the parser 24f's `/admin/rules` page
 * reads, over the one payload the new proxy returns.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 24eb's Deviations):
 * - `web/src/features/admin/rules/schema.ts` exports `sodRuleRoleBlockSchema`,
 *   `sodRuleRowSchema` and `sodRulesBoardSchema`, plus the inferred types
 *   `SodRuleRoleBlock`, `SodRuleRow` and `SodRulesBoard` — decision D: one
 *   file over the ENVELOPE, `killswitch-schema.ts`' own naming.
 * - the envelope key is `rules`, the row carries the SEVEN fields of
 *   `SodRuleRow` (`rule_id`, `role_a_id`, `role_a`, `role_b_id`, `role_b`,
 *   `rationale`, `policy_ref`) and a resolved side the FIVE of
 *   `SodRuleRoleBlock` (`role_id`, `slug`, `name`, `system_slug`,
 *   `system_name`) — all read off `mcp_server/tools/admin.py`, never invented
 *   here.
 * - every field is REQUIRED, and nullable exactly where the tool's own model
 *   is (`role_a` and `role_b` alone), never `.optional()`: §5's third risk is
 *   that a null block reaching the page as `undefined` renders an
 *   unresolvable rule as a resolved one with blank names, which is exactly the
 *   row an admin is on this page to find.
 * - the ids cross as STRINGS: the wire is JSON and the tool's uuids are
 *   serialised, the way `audit-schema.ts` carries its ids.
 * - nothing here is `.strict()` (`killswitch-schema.ts`' reason: the wire may
 *   grow, and a page that refused a payload carrying one new key would break
 *   on a deploy that changed nothing it reads).
 *
 * No backing service, no route, no page: this is the parser alone.
 */

const ROLE_A_ID = "55555555-5555-5555-8555-555555555555";
const ROLE_B_ID = "66666666-6666-5666-8666-666666666666";

/** One resolved side of a rule, every field present, obviously fake. */
const ROLE_BLOCK: Record<string, unknown> = {
  role_id: ROLE_A_ID,
  slug: "fake-payments-approver",
  name: "Fake Payments Approver",
  system_slug: "fake-finance-app",
  system_name: "Obviously Fake Finance App",
};

/** The five field names of the block, written out rather than derived. */
const BLOCK_FIELDS = ["role_id", "slug", "name", "system_slug", "system_name"] as const;

/** A rule with both sides resolved: the ordinary row the page renders in full. */
const RESOLVED_ROW: Record<string, unknown> = {
  rule_id: "cccccccc-cccc-5ccc-8ccc-cccccccccccc",
  role_a_id: ROLE_A_ID,
  role_a: ROLE_BLOCK,
  role_b_id: ROLE_B_ID,
  role_b: {
    ...ROLE_BLOCK,
    role_id: ROLE_B_ID,
    slug: "fake-payments-creator",
    name: "Fake Payments Creator",
  },
  rationale: "Obviously fake rationale, tests only.",
  policy_ref: "FAKE-SOD-001",
};

/** A rule whose `role_a` will not resolve: the block is null, the id stays. */
const HALF_RESOLVED_ROW: Record<string, unknown> = {
  ...RESOLVED_ROW,
  rule_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
  role_a: null,
  policy_ref: "FAKE-SOD-002",
};

/** Neither side resolvable — the row a design note's decision B keeps on the board. */
const UNRESOLVED_ROW: Record<string, unknown> = {
  ...RESOLVED_ROW,
  rule_id: "bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb",
  role_a: null,
  role_b: null,
  policy_ref: "FAKE-SOD-003",
};

/** The seven field names of §1, written out rather than derived from the row. */
const ROW_FIELDS = [
  "rule_id",
  "role_a_id",
  "role_a",
  "role_b_id",
  "role_b",
  "rationale",
  "policy_ref",
] as const;

function without(row: Record<string, unknown>, field: string): Record<string, unknown> {
  const copy = { ...row };
  delete copy[field];
  return copy;
}

function board(...rows: Record<string, unknown>[]): unknown {
  return { rules: rows };
}

describe("sodRulesBoardSchema", () => {
  it("accepts a three-row board: fully resolved, one side null, both null", () => {
    // The premise every rejection row below depends on: a schema that refused
    // the complete payload would make all twelve of them pass while the page
    // rendered nothing at all. The three rows are the three states a design note
    // allows, so a schema that pinned either block to an object would refuse
    // the two rows an admin opened the page to fix. The counts ride along
    // because §1 fixes the row at seven fields and the block at five
    //.
    const parsed = sodRulesBoardSchema.safeParse(
      board(RESOLVED_ROW, HALF_RESOLVED_ROW, UNRESOLVED_ROW),
    );

    expect(parsed.success).toBe(true);
    expect(ROW_FIELDS).toHaveLength(7);
    expect(BLOCK_FIELDS).toHaveLength(5);
    if (!parsed.success) {
      throw new Error("the complete board must parse");
    }
    expect(parsed.data.rules.map((rule) => rule.policy_ref)).toEqual([
      "FAKE-SOD-001",
      "FAKE-SOD-002",
      "FAKE-SOD-003",
    ]);
    expect(parsed.data.rules[1]?.role_a).toBeNull();
    expect(parsed.data.rules[1]?.role_a_id).toBe(ROLE_A_ID);
    expect(parsed.data.rules[2]?.role_b).toBeNull();
  });

  it.each(ROW_FIELDS)("rejects a board row with no %s", (field) => {
    // Catches a parser that declares the field `.optional()` (or omits it
    // altogether). On this surface each of the seven is a different silent lie:
    // an undefined `role_a` renders an unresolvable side as a resolved one with
    // a blank name (§5's third risk), an undefined `role_a_id` leaves the admin
    // with a conflict they cannot look up, an undefined `rationale` or
    // `policy_ref` strips the only answer to "why does this rule exist", and a
    // missing `rule_id` leaves a line nothing can be done about.
    expect(sodRulesBoardSchema.safeParse(board(without(RESOLVED_ROW, field))).success).toBe(false);
  });

  it.each(BLOCK_FIELDS)("rejects a resolved role block with no %s", (field) => {
    // Catches an `.optional()` field inside the block, asserted through the
    // ENVELOPE as well as through the block schema, so a board schema written
    // over a looser row than `sodRuleRowSchema` is caught too. A block missing
    // `name` or `system_name` renders a conflict between two blanks, which is
    // the one thing a separation-of-duties page may not do, and a block missing
    // `slug` or `role_id` leaves nothing to search on.
    expect(sodRuleRoleBlockSchema.safeParse(without(ROLE_BLOCK, field)).success).toBe(false);
    expect(
      sodRulesBoardSchema.safeParse(board({ ...RESOLVED_ROW, role_a: without(ROLE_BLOCK, field) }))
        .success,
    ).toBe(false);
  });

  it("accepts an empty board and rejects a bare array or another envelope key", () => {
    // §1's "a tenant with no rules crosses as `{"rules": []}`". Catches a
    // schema written over the ROW rather than over the envelope (decision D),
    // which would leave the page parsing per row and never seeing the envelope
    // the tool answers, and one that guessed a different key — `rows`, or the
    // `switches` of the file this one is modelled on.
    expect(sodRulesBoardSchema.safeParse({ rules: [] }).success).toBe(true);
    expect(sodRulesBoardSchema.safeParse([RESOLVED_ROW]).success).toBe(false);
    expect(sodRulesBoardSchema.safeParse({ rows: [RESOLVED_ROW] }).success).toBe(false);
  });

  it("accepts a row and an envelope carrying an unknown extra key", () => {
    // Catches a `.strict()` schema. The wire may grow — 23e widened a tool's
    // output by six fields without touching any parser — and a page that
    // refused a payload carrying one new key would break on a deploy that
    // changed nothing it reads.
    expect(
      sodRulesBoardSchema.safeParse(board({ ...RESOLVED_ROW, severity: "high" })).success,
    ).toBe(true);
    expect(
      sodRulesBoardSchema.safeParse({ ...(board(RESOLVED_ROW) as object), read_at: "later" })
        .success,
    ).toBe(true);
  });

  it("rejects numeric ids on the row and inside the block", () => {
    // §1's "ids cross the wire as strings". Catches a schema written with
    // `z.number()` or with `z.coerce.string()` on any id: the tool serialises
    // uuids, so a number is a payload nothing on the server produces, and a
    // coercing parser would silently accept it and hand the page a value no
    // lookup can match. Only the id fields' type can decide this row — every
    // field is present.
    expect(sodRulesBoardSchema.safeParse(board({ ...RESOLVED_ROW, rule_id: 1 })).success).toBe(
      false,
    );
    expect(sodRulesBoardSchema.safeParse(board({ ...RESOLVED_ROW, role_a_id: 1 })).success).toBe(
      false,
    );
    expect(sodRuleRoleBlockSchema.safeParse({ ...ROLE_BLOCK, role_id: 1 }).success).toBe(false);
  });
});

describe("sodRuleRowSchema", () => {
  it("accepts one row on its own, and both nullable sides", () => {
    // The row schema is exported so 24f can narrow a single line without
    // re-parsing the board. Catches a file that exported only the envelope —
    // the page would then have no type for a row at all — and one whose row
    // schema is looser than the envelope's, which the per-field rejections
    // above read through the envelope and this case reads directly.
    expect(sodRuleRowSchema.safeParse(RESOLVED_ROW).success).toBe(true);
    expect(sodRuleRowSchema.safeParse(HALF_RESOLVED_ROW).success).toBe(true);
    expect(sodRuleRowSchema.safeParse(UNRESOLVED_ROW).success).toBe(true);
  });

  it.each(ROW_FIELDS)("rejects a bare row with no %s", (field) => {
    // The same seven rejections against the exported row schema rather than
    // through the envelope: catches an envelope that tightened the row itself
    // (`z.object({...}).required()` around a loose row), which would leave 24f's
    // own per-row narrowing accepting a payload the board rejects.
    expect(sodRuleRowSchema.safeParse(without(RESOLVED_ROW, field)).success).toBe(false);
  });

  it("rejects a role block replaced by a string or by undefined", () => {
    // `role_a` is a BLOCK or `null`, never a name: catches a schema that
    // flattened the side to `z.string()` (the page would lose the system the
    // role lives in) and one that wrote `.nullish()` or `.optional()` instead
    // of `.nullable()` — the distinction §5's third risk turns on, and the one
    // an `.optional()` field passes every rejection above by not being there.
    expect(
      sodRuleRowSchema.safeParse({ ...RESOLVED_ROW, role_a: "Fake Payments Approver" }).success,
    ).toBe(false);
    expect(sodRuleRowSchema.safeParse({ ...RESOLVED_ROW, role_a: undefined }).success).toBe(false);
  });
});
