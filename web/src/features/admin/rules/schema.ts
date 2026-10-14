/**
 * The parser for what `/api/admin/rules` returns.
 *
 * Written over the ENVELOPE rather than over a bare row (decision D), the way
 * `killswitch-schema.ts`, `audit-schema.ts` and `reviews-schema.ts` carry
 * theirs: an earlier change runs ONE `safeParse` on the proxy's body and gets either the
 * whole board or one refusal, never a half-parsed list it has to re-check per
 * row.
 *
 * Every field is REQUIRED, and nullable exactly where the tool's own model is —
 * the two resolved sides alone. An `.optional()` there would reach the page as
 * `undefined`, which renders an unresolvable rule as a resolved one with blank
 * names: exactly the row an admin is on this page to find (§5's third risk).
 * `tests/rules-schema.test.ts` rejects a row missing any one of its seven and a
 * block missing any one of its five, one case per field.
 *
 * The ids cross as STRINGS and are never coerced: the wire is JSON and the
 * tool's uuids are already serialised, so a `z.coerce.string()` would accept a
 * number and hand the page an id no link can resolve.
 *
 * Nothing here is `.strict()`. The wire may grow, and a page that refused a
 * payload carrying one new key would break on a deploy that changed nothing it
 * reads.
 */

import { z } from "zod";

/**
 * One side of a rule, resolved: `SodRuleRoleBlock`'s five fields.
 *
 * The role and the system it belongs to, in words. Both halves are carried
 * because a role slug alone does not say which system grants it, and two
 * systems may name a role the same thing.
 */
export const sodRuleRoleBlockSchema = z.object({
  role_id: z.string(),
  slug: z.string(),
  name: z.string(),
  system_slug: z.string(),
  system_name: z.string(),
});

/**
 * One rule on the board: `SodRuleRow`'s seven fields.
 *
 * Both ids are always present and each resolved block may be `null`, which is
 * the whole of 24ea decision B: nullability is the marker that a side could not
 * be read, and no flag rides beside it because there is no second fact to say.
 * `.nullable()` and never `.nullish()`, so a missing key is a rejection rather
 * than an unreadable side silently reported as one.
 */
export const sodRuleRowSchema = z.object({
  rule_id: z.string(),
  role_a_id: z.string(),
  role_a: sodRuleRoleBlockSchema.nullable(),
  role_b_id: z.string(),
  role_b: sodRuleRoleBlockSchema.nullable(),
  rationale: z.string(),
  policy_ref: z.string(),
});

/**
 * The whole board: every rule the tenant has, in the tool's own order
 * (`policy_ref` ascending, then `rule_id` ascending — 24ea decision C).
 *
 * An empty `rules` is a valid payload — a tenant with no rules is a 200, never
 * a 404 — and a bare array is not: the envelope is the unit the page parses.
 */
export const sodRulesBoardSchema = z.object({
  rules: z.array(sodRuleRowSchema),
});

export type SodRuleRoleBlock = z.infer<typeof sodRuleRoleBlockSchema>;
export type SodRuleRow = z.infer<typeof sodRuleRowSchema>;
export type SodRulesBoard = z.infer<typeof sodRulesBoardSchema>;
