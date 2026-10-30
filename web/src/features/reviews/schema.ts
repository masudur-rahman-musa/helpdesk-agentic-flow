/**
 * Parsers for the review queue, item list and decision answer. Every field is
 * required, so a dropped column is refused instead of showing as a blank cell.
 * Nothing is `.strict()`, so a new server key does not break the page.
 */

import { z } from "zod";

/** One review of the queue: the thirteen `ReviewQueueRow` fields. */
export const reviewQueueRowSchema = z.object({
  review_id: z.string(),
  period: z.string(),
  status: z.string(),
  due_at: z.string().nullable(),
  completed_at: z.string().nullable(),
  department_id: z.string().nullable(),
  department_name: z.string().nullable(),
  system_id: z.string().nullable(),
  system_name: z.string().nullable(),
  reviewer_user_id: z.string(),
  reviewer_display_name: z.string(),
  item_count: z.number(),
  decided_count: z.number(),
});

export const reviewQueueSchema = z.object({
  reviews: z.array(reviewQueueRowSchema),
});

/** One review item: the eleven `ReviewItemOutput` fields. */
export const reviewItemSchema = z.object({
  item_id: z.string(),
  grant_id: z.string(),
  flag: z.string(),
  decision: z.string().nullable(),
  decided_at: z.string().nullable(),
  target_display_name: z.string(),
  role_name: z.string(),
  system_name: z.string(),
  privilege: z.string(),
  granted_at: z.string(),
  expires_at: z.string().nullable(),
});

/**
 * One page of the item list. `review_id` comes along so the list can be tied
 * to its review. `next_after_item_id` is where the next page starts; null or
 * absent means this page is the last.
 */
export const reviewItemsSchema = z.object({
  review_id: z.string(),
  items: z.array(reviewItemSchema),
  next_after_item_id: z.string().nullable().optional(),
});

/**
 * `decide_review_item`'s answer. `revoked_grant_count` says whether Revoke
 * ended a grant or found it gone; `review_complete` is the only record that a
 * review closed.
 */
export const reviewDecisionSchema = z.object({
  item_id: z.string(),
  review_id: z.string(),
  decision: z.string(),
  decided_at: z.string().nullable(),
  revoked_grant_count: z.number(),
  review_complete: z.boolean(),
});

export type ReviewQueueRow = z.infer<typeof reviewQueueRowSchema>;
export type ReviewItem = z.infer<typeof reviewItemSchema>;
export type ReviewItemsPage = z.infer<typeof reviewItemsSchema>;
export type ReviewDecision = z.infer<typeof reviewDecisionSchema>;
