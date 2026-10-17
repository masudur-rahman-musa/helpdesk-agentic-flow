import { describe, expect, it } from "vitest";

import {
  killSwitchBoardSchema,
  setKillSwitchResultSchema,
  switchStateSchema,
} from "@/features/admin/killswitch/schema";

/**
 * `killswitch-schema.test.ts`: the parser the
 * `/admin/killswitch` page reads, over the two payloads the new proxy returns.
 *
 * Shapes this file fixes:
 * - `web/src/features/admin/killswitch/schema.ts` exports `switchStateSchema`,
 *   `killSwitchRowSchema`, `killSwitchBoardSchema` and
 *   `setKillSwitchResultSchema`, plus the inferred types `SwitchState`,
 *   `KillSwitchRow`, `KillSwitchBoard` and `SetKillSwitchResult` in
 *   one file, both envelopes, `audit-schema.ts`' own naming.
 * - the board envelope is `{ switches: [...] }` with the SIX `KillSwitchRow`
 *   fields (`tool`, `protected`, `stopped`, `global_state`, `tenant_state`,
 *   `unreadable`), a `SwitchState` being `{ reason, actor, at }` — all read off
 *   `mcp_server/tools/admin.py` and `helpdesk/killswitch/switch.py`, never
 *   invented here.
 * - the set result is `{ tool_name, stopped, scope, changed_at }`, read off
 *   `SetKillSwitchOutput`.
 * - every field is REQUIRED, and nullable exactly where the tool's own model is
 *   (`global_state` and `tenant_state` alone), never `.optional()`: a dropped
 *   field would otherwise reach the page as `undefined`, and on THIS surface an
 *   undefined `stopped` or `protected` renders as a tool that looks like it is
 *   running, or like it may be stopped when no switch may touch it.
 * - `at` and `changed_at` are strings, the way `audit-schema.ts` carries
 *   `occurred_at`: the wire is JSON and nothing here reads a `Date`.
 * - no schema is `.strict()`: an unknown extra key is accepted, because the
 *   wire may grow and a page that refuses a widened payload breaks on a deploy
 *   that changed nothing it reads.
 *
 * No backing service, no route, no page: this is the parser alone.
 */

const ACTOR = "22222222-2222-5222-8222-222222222222";

/** One stored switch state, every field present, obviously fake. */
const SWITCH_STATE: Record<string, unknown> = {
  reason: "Obviously fake platform stop.",
  actor: ACTOR,
  at: "2026-09-12T08:00:00Z",
};

const SWITCH_STATE_FIELDS = ["reason", "actor", "at"] as const;

/** A tool nothing has stopped: both layers null, running, not protected. */
const CLEAN_ROW: Record<string, unknown> = {
  tool: "grant_access",
  protected: false,
  stopped: false,
  global_state: null,
  tenant_state: null,
  unreadable: false,
};

/** A tool stopped on BOTH layers, which is the row the two-layer design exists for. */
const STOPPED_ROW: Record<string, unknown> = {
  tool: "revoke_access",
  protected: true,
  stopped: true,
  global_state: SWITCH_STATE,
  tenant_state: { ...SWITCH_STATE, reason: "Obviously fake tenant stop." },
  unreadable: false,
};

/** The six field names, written out rather than derived from the row. */
const ROW_FIELDS = [
  "tool",
  "protected",
  "stopped",
  "global_state",
  "tenant_state",
  "unreadable",
] as const;

/** One `set_kill_switch` answer, every field present. */
const SET_RESULT: Record<string, unknown> = {
  tool_name: "grant_access",
  stopped: true,
  scope: "global",
  changed_at: "2026-09-13T10:15:00Z",
};

const SET_RESULT_FIELDS = ["tool_name", "stopped", "scope", "changed_at"] as const;

function without(row: Record<string, unknown>, field: string): Record<string, unknown> {
  const copy = { ...row };
  delete copy[field];
  return copy;
}

function board(...rows: Record<string, unknown>[]): unknown {
  return { switches: rows };
}

describe("killSwitchBoardSchema", () => {
  it("accepts a two-row board, one clean and one stopped on both layers", () => {
    // The premise every rejection row below depends on: a schema that refused
    // the complete payload would make all six of them pass while the page
    // rendered nothing at all. The count is asserted beside it because the row
    // has six fields.
    expect(killSwitchBoardSchema.safeParse(board(CLEAN_ROW, STOPPED_ROW)).success).toBe(true);
    expect(ROW_FIELDS).toHaveLength(6);
  });

  it.each(ROW_FIELDS)("rejects a board row with no %s", (field) => {
    // Catches a parser that declares the field `.optional()` (or omits it
    // altogether). On this surface each of the six is a different silent lie:
    // an undefined `stopped` renders a stopped tool as running, an undefined
    // `protected` renders an enabled toggle for the one tool no switch may
    // stop, an undefined `unreadable` hides the only row an operator has to act
    // on, and a missing `tool` leaves a line naming nothing at all.
    expect(killSwitchBoardSchema.safeParse(board(without(CLEAN_ROW, field))).success).toBe(false);
  });

  it("accepts a null global_state, a null tenant_state, and both null", () => {
    // Accepts a null `global_state` and a null `tenant_state`.
    // Catches a parser pinning either layer to an object: a tool stopped for
    // one tenant alone carries `global_state: null`, and a tool nothing has
    // stopped carries both null — which is most of the board on most days, so
    // the page would refuse the ordinary payload on every load.
    expect(
      killSwitchBoardSchema.safeParse(board({ ...STOPPED_ROW, global_state: null })).success,
    ).toBe(true);
    expect(
      killSwitchBoardSchema.safeParse(board({ ...STOPPED_ROW, tenant_state: null })).success,
    ).toBe(true);
    expect(killSwitchBoardSchema.safeParse(board(CLEAN_ROW)).success).toBe(true);
  });

  it("accepts an unreadable row with both states null", () => {
    // Accepts `unreadable: true` with both states null:
    // a stored value that will not parse is REPORTED rather than raised, and
    // this board is the only surface from which a bad key can be seen and
    // cleared. Catches a parser that requires a state whenever a row is
    // stopped — the one row an operator cannot afford to have refused would be
    // the one the page drops.
    const unreadable = {
      ...CLEAN_ROW,
      stopped: true,
      unreadable: true,
      global_state: null,
      tenant_state: null,
    };

    expect(killSwitchBoardSchema.safeParse(board(unreadable)).success).toBe(true);
  });

  it("accepts an empty board and rejects a bare array", () => {
    // The envelope is the unit the page parses, so a board with no rows is a
    // valid payload and a naked list is not. Catches a schema written over the
    // ROW rather than over the envelope, which would leave the page parsing per
    // row and never seeing the envelope the tool actually answers.
    expect(killSwitchBoardSchema.safeParse({ switches: [] }).success).toBe(true);
    expect(killSwitchBoardSchema.safeParse([CLEAN_ROW]).success).toBe(false);
    expect(killSwitchBoardSchema.safeParse({ rows: [CLEAN_ROW] }).success).toBe(false);
  });

  it("rejects string booleans on protected, stopped and unreadable", () => {
    // All three are BOOLEANS: `"false"` is truthy in the page's own
    // conditional, so a coerced string would render a running tool as stopped,
    // or enable the toggle on a protected tool. Catches a schema written with
    // `z.coerce.boolean()` or with `z.unknown()` on any of the three.
    for (const field of ["protected", "stopped", "unreadable"]) {
      expect(
        killSwitchBoardSchema.safeParse(board({ ...CLEAN_ROW, [field]: "false" })).success,
      ).toBe(false);
    }
  });

  it("accepts a row and an envelope carrying an unknown extra key", () => {
    // Catches a `.strict()` schema. The wire may grow: a tool can widen its
    // output by six fields without touching any parser — and a page that
    // refused a payload carrying one new key would break on a deploy that
    // changed nothing it reads.
    expect(killSwitchBoardSchema.safeParse(board({ ...CLEAN_ROW, since: "later" })).success).toBe(
      true,
    );
    expect(
      killSwitchBoardSchema.safeParse({ ...(board(CLEAN_ROW) as object), read_at: "later" })
        .success,
    ).toBe(true);
  });

  it("exposes both layers, protected and stopped on the parsed value", () => {
    // Catches fields parsed as `z.unknown()`: the two layers are what the page
    // renders, `protected` is what disables the toggle and `stopped` is the
    // line's own answer. Under strict TypeScript this case does not compile if
    // any of them is unreadable.
    const parsed = killSwitchBoardSchema.safeParse(board(STOPPED_ROW));
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    const row = parsed.data.switches[0];
    expect(row?.tool).toBe("revoke_access");
    expect(row?.protected).toBe(true);
    expect(row?.stopped).toBe(true);
    expect(row?.global_state?.reason).toBe("Obviously fake platform stop.");
    expect(row?.tenant_state?.actor).toBe(ACTOR);
    expect(row?.unreadable).toBe(false);
  });
});

describe("switchStateSchema", () => {
  it("accepts the whole three-field state", () => {
    // The premise the three rejection rows depend on: a schema refusing the
    // complete state would make all of them pass while the page showed no
    // provenance for any stopped tool.
    expect(switchStateSchema.safeParse(SWITCH_STATE).success).toBe(true);
    expect(SWITCH_STATE_FIELDS).toHaveLength(3);
  });

  it.each(SWITCH_STATE_FIELDS)("rejects a switch state with no %s", (field) => {
    // Catches an `.optional()` field on the one payload that is audit
    // evidence: the three together are why a tool is off, who turned it off
    // and when, and a state missing any one of them renders a stop nobody
    // owns. The store's own model is frozen and `extra="forbid"` for exactly
    // this reason, so a partial state is a value written by something other
    // than the switch.
    expect(switchStateSchema.safeParse(without(SWITCH_STATE, field)).success).toBe(false);
    expect(
      killSwitchBoardSchema.safeParse(
        board({ ...STOPPED_ROW, global_state: without(SWITCH_STATE, field) }),
      ).success,
    ).toBe(false);
  });

  it("accepts a state carrying an unknown extra key", () => {
    // The `.strict()` catch again. The stored model forbids extras on the
    // SERVER, where a surprising field is a reason to deny; the page is the
    // wrong place to repeat that judgement, because it would refuse a board
    // that a widened `SwitchState` made perfectly readable.
    expect(switchStateSchema.safeParse({ ...SWITCH_STATE, scope: "global" }).success).toBe(true);
  });
});

describe("setKillSwitchResultSchema", () => {
  it("accepts the whole four-field result and both polarities", () => {
    // Accepts the four-field set result. `stopped: false` is the release
    // half, and a parser that only ever saw a stop would
    // refuse the answer to letting a tool run again.
    expect(setKillSwitchResultSchema.safeParse(SET_RESULT).success).toBe(true);
    expect(setKillSwitchResultSchema.safeParse({ ...SET_RESULT, stopped: false }).success).toBe(
      true,
    );
    expect(SET_RESULT_FIELDS).toHaveLength(4);
  });

  it.each(SET_RESULT_FIELDS)("rejects a set result with no %s", (field) => {
    // Catches an `.optional()` field on the answer to a MUTATION: an undefined
    // `stopped` is the toggle unable to say whether the tool it just acted on
    // is now off, which on this control is the one thing the admin clicked to
    // find out, and an undefined `scope` leaves them unable to tell a
    // platform-wide stop from their own tenant's.
    expect(setKillSwitchResultSchema.safeParse(without(SET_RESULT, field)).success).toBe(false);
  });

  it("rejects a string stopped and a non-string scope", () => {
    // `stopped` is a BOOLEAN: `"false"` is truthy, so a coerced string would
    // report a released tool as stopped and invert the very polarity this
    // schema exists to protect. `scope` is a STRING carrying either `global` or a
    // tenant id, so a parser expecting a boolean or a number cannot hold the
    // answer at all.
    expect(setKillSwitchResultSchema.safeParse({ ...SET_RESULT, stopped: "true" }).success).toBe(
      false,
    );
    expect(setKillSwitchResultSchema.safeParse({ ...SET_RESULT, scope: 1 }).success).toBe(false);
  });

  it("accepts a result carrying an unknown extra key", () => {
    // The `.strict()` catch, for the same reason as the board envelope.
    expect(setKillSwitchResultSchema.safeParse({ ...SET_RESULT, actor: ACTOR }).success).toBe(true);
  });

  it("exposes stopped, scope and changed_at on the parsed value", () => {
    // Catches fields parsed as `z.unknown()`: these are what the toggle
    // renders back to the admin who clicked it. Under strict TypeScript this
    // case does not compile if any of the three is unreadable.
    const parsed = setKillSwitchResultSchema.safeParse(SET_RESULT);
    if (!parsed.success) {
      throw new Error("the complete payload must parse");
    }

    expect(parsed.data.tool_name).toBe("grant_access");
    expect(parsed.data.stopped).toBe(true);
    expect(parsed.data.scope).toBe("global");
    expect(parsed.data.changed_at).toBe("2026-09-13T10:15:00Z");
  });
});
