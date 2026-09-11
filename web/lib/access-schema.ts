/**
 * The parsers for what `/api/me/requests` and `/api/me/access` return.
 *
 * Written over the ENVELOPE rather than over a bare row, so a page runs one
 * `safeParse` on the proxy's body and gets either the whole payload or one
 * refusal — never a half-parsed list it has to check again per row.
 *
 * Every field the tool promises is REQUIRED here, and nullable only where the
 * tool can answer null (`requested_expiry_days`, `grant_id`, `expires_at`, and
 * an unresolved approval seat's `user_id`). An `.optional()` field would let a
 * dropped column reach the page as `undefined` and render as a blank cell
 * instead of a refused payload; `tests/access-schema.test.ts` rejects a row
 * with any one of the nineteen missing, one case per field.
 *
 * Nothing here is `.strict()`. The wire may grow — this plan is itself a
 * widening of the same tool — and a page that refused a payload carrying one
 * new key would break on a deploy that changed nothing it reads. Pinned by the
 * "accepts a row carrying an unknown extra field" cases.
 *
 * Dates and ids stay STRINGS. They arrive as JSON, the pages render them, and
 * coercing here would put a second opinion about the wire's shape in front of
 * the one that produced it.
 */

import { z } from "zod";

/**
 * One seat of an approval chain, rendered BY SLOT.
 *
 * `user_id` is null for a seat nobody resolved — a group seat such as `hr` or
 * `it_admin` names no one — and no display name is resolved for a seat that
 * does name someone, because that would be a second directory read per row for
 * a label the slot already carries.
 */
export const approvalSlotSchema = z.object({
  slot: z.string(),
  user_id: z.string().nullable(),
});

/** One request row: the nineteen fields of an earlier change §1. */
export const myRequestSchema = z.object({
  request_id: z.string(),
  target_user_id: z.string(),
  role_id: z.string(),
  justification: z.string(),
  requested_expiry_days: z.number().nullable(),
  risk_class: z.string(),
  decision: z.string(),
  status: z.string(),
  created_at: z.string(),
  role_slug: z.string(),
  role_name: z.string(),
  system_slug: z.string(),
  system_name: z.string(),
  target_display_name: z.string(),
  decision_reasons: z.array(z.string()),
  citations: z.array(z.unknown()),
  approvals: z.array(approvalSlotSchema),
  grant_id: z.string().nullable(),
  expires_at: z.string().nullable(),
});

export const myRequestsSchema = z.object({
  requests: z.array(myRequestSchema),
});

/** One live grant, exactly as `list_my_access` has answered since an earlier change. */
export const myGrantSchema = z.object({
  grant_id: z.string(),
  role_slug: z.string(),
  role_name: z.string(),
  privilege: z.string(),
  system_slug: z.string(),
  system_name: z.string(),
  granted_at: z.string(),
  expires_at: z.string().nullable(),
});

export const myAccessSchema = z.object({
  grants: z.array(myGrantSchema),
});

export type MyRequest = z.infer<typeof myRequestSchema>;
export type MyGrant = z.infer<typeof myGrantSchema>;
