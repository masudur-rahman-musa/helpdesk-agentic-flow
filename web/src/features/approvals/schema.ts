/**
 * Parser for the approver's queue. Every field is required, so a dropped
 * column fails the parse. Seat and citation shapes are shared with requests,
 * since both read the same stored data.
 */

import { z } from "zod";

import { approvalSlotSchema, requestCitationSchema } from "@/features/access/schema";

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
