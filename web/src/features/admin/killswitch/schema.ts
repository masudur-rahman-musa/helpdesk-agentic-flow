/**
 * Parsers for the kill switch board and toggle. Every field is required: a
 * missing `stopped` or `protected` would show a stopped tool as running or
 * enable a toggle that must stay off.
 */

import { z } from "zod";

/** Why, who and when. All required: a stop with no owner is no record. */
export const switchStateSchema = z.object({
  reason: z.string(),
  actor: z.string(),
  at: z.string(),
});

/**
 * Booleans are never coerced: `"false"` is truthy, so a stopped tool would
 * look running or a protected tool would get a live toggle.
 */
export const killSwitchRowSchema = z.object({
  tool: z.string(),
  protected: z.boolean(),
  stopped: z.boolean(),
  global_state: switchStateSchema.nullable(),
  tenant_state: switchStateSchema.nullable(),
  unreadable: z.boolean(),
});

export const killSwitchBoardSchema = z.object({
  switches: z.array(killSwitchRowSchema),
});

/** `scope` is `global` or the tenant id the switch was written for. */
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
