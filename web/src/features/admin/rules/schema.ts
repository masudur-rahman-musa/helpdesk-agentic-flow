/**
 * Parser for the rules board. Every field is required: a missing role block
 * would show an unresolved rule as resolved with blank names.
 */

import { z } from "zod";

export const sodRuleRoleBlockSchema = z.object({
  role_id: z.string(),
  slug: z.string(),
  name: z.string(),
  system_slug: z.string(),
  system_name: z.string(),
});

/** A `null` block means that side could not be resolved. A missing key fails. */
export const sodRuleRowSchema = z.object({
  rule_id: z.string(),
  role_a_id: z.string(),
  role_a: sodRuleRoleBlockSchema.nullable(),
  role_b_id: z.string(),
  role_b: sodRuleRoleBlockSchema.nullable(),
  rationale: z.string(),
  policy_ref: z.string(),
});

export const sodRulesBoardSchema = z.object({
  rules: z.array(sodRuleRowSchema),
});

export type SodRuleRoleBlock = z.infer<typeof sodRuleRoleBlockSchema>;
export type SodRuleRow = z.infer<typeof sodRuleRowSchema>;
export type SodRulesBoard = z.infer<typeof sodRulesBoardSchema>;
