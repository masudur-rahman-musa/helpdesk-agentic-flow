/**
 * The parser for what the two `/api/admin/audit/...` proxies return.
 *
 * One file for both envelopes (decision E), the way `reviews-schema.ts` carries
 * its three: an earlier change renders a page of events and the chain's integrity on one
 * screen, and a reader checks a row's `hash` against the verification without a
 * second trip.
 *
 * Each schema is written over the ENVELOPE rather than over a bare row, so the
 * page runs ONE `safeParse` on the proxy's body and gets either the whole
 * payload or one refusal, never a half-parsed list it has to check again per
 * row.
 *
 * Every field is REQUIRED, and nullable exactly where the tool's own model is:
 * an event's `actor_ref` and `subject_id` (a system actor has no reference and
 * some subjects have no id), the page's `next_before_seq`, and the
 * verification's `broken_at`, `truncated_at` and `anchored_seq`. An
 * `.optional()` field would let a dropped column
 * reach the page as `undefined` and render as a blank cell instead of a refused
 * payload; `tests/audit-schema.test.ts` rejects an event row with any one of
 * the nine missing, one case per field.
 *
 * Nothing here is `.strict()`. The wire may grow, and a page that refused a
 * payload carrying one new key would break on a deploy that changed nothing it
 * reads.
 */

import { z } from "zod";

/**
 * One stored event as `search_audit` returns it: the nine `AuditEventRow`
 * fields.
 *
 * `payload` is an arbitrary object by design (13b decision E) — it carries
 * whatever the event that wrote it recorded — so it is a record of unknowns
 * rather than a shape. A schema demanding keys would reject real rows, and one
 * written as `z.unknown()` would let a string or a null through into a page
 * that renders entries.
 *
 * `seq` and `next_before_seq` are numbers rather than strings: `seq` is the
 * cursor the next page is asked for by, and a string that looked like one would
 * page the log wrongly rather than visibly.
 */
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
 * One page of the log.
 *
 * `next_before_seq` is required and nullable, never optional: 13b's scope
 * filter runs after the read, so a narrow caller's page can be short or empty
 * while matching events exist further back, and this cursor is the only way to
 * reach them. A parser that let it arrive as `undefined` would leave 24b unable
 * to tell "the end of the log" from "the proxy dropped the cursor".
 */
export const auditSearchSchema = z.object({
  events: z.array(auditEventRowSchema),
  next_before_seq: z.number().nullable(),
});

/**
 * An earlier change's `GET /admin/audit/verify` answer, as an earlier change left it: four
 * fields, not two.
 *
 * `broken_at` is the `seq` of the first event whose hash does not follow from
 * the one before it, and null on an intact chain — so `ok: false` with a number
 * is the only shape that says where to look.
 *
 * `truncated_at` is the `seq` of the first event that is GONE: rows removed
 * from the tail leave no broken link behind them, so the anchored head is the
 * only thing that notices, and this is a separate fact from `broken_at` rather
 * than a second way of spelling it (30bb decision E).
 *
 * `anchored_seq` is the head the check was measured against, and null where
 * this tenant has never anchored one — which makes `ok: true` a weaker claim,
 * because an unanchored chain cannot report a removed tail at all.
 *
 * All three are REQUIRED and nullable, never `.optional()`: an absent
 * `anchored_seq` arriving as `undefined` would render as "never anchored",
 * which is a different claim about the system than "the proxy dropped the
 * field". `tests/audit-schema.test.ts::rejects a four-field answer missing %s`
 * refuses each of the four in turn.
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
