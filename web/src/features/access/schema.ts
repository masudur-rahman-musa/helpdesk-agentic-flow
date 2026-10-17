/**
 * Parsers for `/api/me/requests` and `/api/me/access`. Every field is required,
 * so a dropped column fails the parse instead of rendering blank. Nothing is
 * `.strict()`, so a new server field does not break the page.
 */

import { z } from "zod";

/** One approval seat. `user_id` is null for a group seat such as `hr`. */
export const approvalSlotSchema = z.object({
  slot: z.string(),
  user_id: z.string().nullable(),
});

/**
 * A citation as stored on a request, not the chat shape: `heading_path` is a
 * string and there is no quoted text, since the request keeps only the source.
 */
export const requestCitationSchema = z.object({
  document_slug: z.string(),
  title: z.string(),
  version: z.string(),
  heading_path: z.string(),
  chunk_id: z.string(),
});

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
  citations: z.array(requestCitationSchema),
  approvals: z.array(approvalSlotSchema),
  grant_id: z.string().nullable(),
  expires_at: z.string().nullable(),
});

export const myRequestsSchema = z.object({
  requests: z.array(myRequestSchema),
});

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

export type RequestCitation = z.infer<typeof requestCitationSchema>;
export type MyRequest = z.infer<typeof myRequestSchema>;
export type MyGrant = z.infer<typeof myGrantSchema>;
