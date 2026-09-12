/**
 * The parser for what the two `/api/admin/killswitch` verbs return.
 *
 * One file for both envelopes (decision F), the way `audit-schema.ts` and
 * `reviews-schema.ts` carry theirs: an earlier change renders the board and the answer
 * to a toggle on one screen, so a click updates a line without a second trip
 * to a second parser.
 *
 * Each schema is written over the ENVELOPE rather than over a bare row, so the
 * page runs ONE `safeParse` on the proxy's body and gets either the whole
 * board or one refusal, never a half-parsed list it has to check again per row.
 *
 * Every field is REQUIRED, and nullable exactly where the tool's own model is —
 * the two layers alone. An `.optional()` field would reach the page as
 * `undefined`, and on THIS surface each of the six is a different silent lie:
 * an undefined `stopped` renders a stopped tool as running, an undefined
 * `protected` renders an enabled toggle for the one tool no switch may stop,
 * and an undefined `unreadable` hides the only row an operator has to act on.
 * `tests/killswitch-schema.test.ts` rejects a row with any one of the six
 * missing, one case per field.
 *
 * Nothing here is `.strict()`. The wire may grow, and a page that refused a
 * payload carrying one new key would break on a deploy that changed nothing it
 * reads.
 */

import { z } from "zod";

/**
 * Why a tool is off, who turned it off, and when: `helpdesk.killswitch`'s
 * stored `SwitchState`, unchanged.
 *
 * All three are required. They are audit evidence together — a state missing
 * any one of them renders a stop nobody owns — which is the same reason the
 * stored model is frozen and forbids extras on the server. The page is the
 * wrong place to repeat that second judgement, so this schema is not strict.
 *
 * `at` is a string, the way `audit-schema.ts` carries `occurred_at`: the wire
 * is JSON and nothing here reads a `Date`.
 */
export const switchStateSchema = z.object({
  reason: z.string(),
  actor: z.string(),
  at: z.string(),
});

/**
 * One tool's line on the board: `KillSwitchRow`'s six fields.
 *
 * Both layers are carried rather than one effective state (24ca decision B):
 * an admin clearing the tenant switch has to be told the global one still
 * holds the tool down. `stopped` rides beside them so 24d renders a line
 * without re-implementing the precedence, and `protected` so the toggle is
 * disabled from the row rather than from a second copy of a security-relevant
 * list.
 *
 * The three booleans are booleans and never coerced: `"false"` is truthy in a
 * page's own conditional, so a coerced string would render a running tool as
 * stopped, or enable the toggle on a tool no switch may touch.
 */
export const killSwitchRowSchema = z.object({
  tool: z.string(),
  protected: z.boolean(),
  stopped: z.boolean(),
  global_state: switchStateSchema.nullable(),
  tenant_state: switchStateSchema.nullable(),
  unreadable: z.boolean(),
});

/**
 * The whole board: one row per registered tool, in the registry's own order.
 *
 * An empty `switches` is a valid payload and a bare array is not — the
 * envelope is the unit the page parses.
 */
export const killSwitchBoardSchema = z.object({
  switches: z.array(killSwitchRowSchema),
});

/**
 * What one toggle answered: `SetKillSwitchOutput`'s four fields.
 *
 * `stopped` describes the TOOL where the request's `enabled` described the
 * SWITCH (13c decision B), so it is a boolean and never coerced: a `"false"`
 * read as true would invert the polarity section 5's first risk is about, on
 * the one control where the wrong direction stops a tool somebody meant to
 * release.
 *
 * `scope` is a string carrying either `global` or the tenant id the switch was
 * written for, which is what tells an admin apart a platform-wide stop from
 * their own tenant's.
 */
export const setKillSwitchResultSchema = z.object({
  tool_name: z.string(),
  stopped: z.boolean(),
  scope: z.string(),
  changed_at: z.string(),
});

export type SwitchState = z.infer<typeof switchStateSchema>;
export type KillSwitchRow = z.infer<typeof killSwitchRowSchema>;
export type KillSwitchBoard = z.infer<typeof killSwitchBoardSchema>;
export type SetKillSwitchResult = z.infer<typeof setKillSwitchResultSchema>;
