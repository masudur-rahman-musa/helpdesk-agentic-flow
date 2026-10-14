/**
 * The parser for what `/api/me/approvals` returns.
 *
 * Written over the ENVELOPE rather than over a bare row, the same shape
 * `access-schema.ts` has, so 23d's page runs one `safeParse` on the proxy's
 * body and gets either the whole queue or one refusal — never a half-parsed
 * list it has to check again per row.
 *
 * Every one of the twenty fields is REQUIRED, and nullable only where the tool
 * can answer null (`requested_expiry_days`, and an unresolved seat's
 * `user_id`). An `.optional()` field would let a dropped column reach the page
 * as `undefined` and render as a blank cell instead of a refused payload;
 * `tests/approvals-schema.test.ts` rejects a row with any one of the twenty
 * missing, one case per field.
 *
 * Nothing here is `.strict()`. The wire may grow — this plan is itself a
 * widening of the same tool — and a page that refused a payload carrying one
 * new key would break on a deploy that changed nothing it reads.
 *
 * The seat and the citation shapes are `access-schema.ts`' own: one queue row
 * and one request row carry the SAME stored chain and the same stored
 * citations, so a second declaration here would be two opinions about one
 * JSONB column.
 */

import { z } from "zod";

import { approvalSlotSchema, requestCitationSchema } from "@/features/access/schema";

/** One pending approval: the twenty fields of an earlier change §1. */
export const pendingApprovalSchema = z.object({
  request_id: z.string(),
  requester_user_id: z.string(),
  target_user_id: z.string(),
  role_id: z.string(),
  justification: z.string(),
  requested_expiry_days: z.number().nullable(),
  risk_class: z.string(),
  decision: z.string(),
  status: z.string(),
  created_at: z.string(),
  my_slot: z.string(),
  approvals: z.array(approvalSlotSchema),
  role_slug: z.string(),
  role_name: z.string(),
  system_slug: z.string(),
  system_name: z.string(),
  target_display_name: z.string(),
  requester_display_name: z.string(),
  decision_reasons: z.array(z.string()),
  citations: z.array(requestCitationSchema),
});

export const pendingApprovalsSchema = z.object({
  approvals: z.array(pendingApprovalSchema),
});

export type PendingApproval = z.infer<typeof pendingApprovalSchema>;
