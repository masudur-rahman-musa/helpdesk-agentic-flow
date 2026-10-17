/**
 * Parsers for the audit search and chain check. Every field is required, so a
 * dropped field fails the parse instead of rendering blank.
 */

import { z } from "zod";

/** `payload` differs per event type, so only "an object" is checked. */
export const auditEventRowSchema = z.object({
  seq: z.number(),
  occurred_at: z.string(),
  actor_kind: z.string(),
  actor_ref: z.string().nullable(),
  event_type: z.string(),
  subject_type: z.string(),
  subject_id: z.string().nullable(),
  payload: z.record(z.string(), z.unknown()),
  hash: z.string(),
});

/**
 * `next_before_seq` must be present even when null: a short page can still
 * have older events, and only the cursor reaches them.
 */
export const auditSearchSchema = z.object({
  events: z.array(auditEventRowSchema),
  next_before_seq: z.number().nullable(),
});

/**
 * `broken_at` is the first event whose hash does not follow. `truncated_at` is
 * the first event that is gone from the tail. `anchored_seq` is the head it was
 * checked against; with none, a removed tail cannot show.
 */
export const auditVerifySchema = z.object({
  ok: z.boolean(),
  broken_at: z.number().nullable(),
  truncated_at: z.number().nullable(),
  anchored_seq: z.number().nullable(),
});

export type AuditEventRow = z.infer<typeof auditEventRowSchema>;
export type AuditSearch = z.infer<typeof auditSearchSchema>;
export type AuditVerify = z.infer<typeof auditVerifySchema>;
