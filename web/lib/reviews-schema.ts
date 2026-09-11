/**
 * The parser for what the three `/api/...` review proxies return.
 *
 * One file for all three envelopes (decision E), the way `approvals-schema.ts`
 * carries its queue: the queue, the item list and the decision are one tool
 * family whose shapes reference each other — a queue row names the review an
 * item list belongs to, and a decision names both.
 *
 * Each schema is written over the ENVELOPE rather than over a bare row, so
 * 23f's page runs ONE `safeParse` on the proxy's body and gets either the whole
 * payload or one refusal, never a half-parsed list it has to check again per
 * row.
 *
 * Every field is REQUIRED, and nullable exactly where the tool's own model is:
 * `due_at` and `completed_at`, both scope id/name pairs (`access_reviews`
 * allows exactly one scope id, so one pair is null on every row), an undecided
 * item's `decision` and `decided_at`, an open-ended grant's `expires_at`, and
 * the decision answer's `decided_at`. An `.optional()` field would let a
 * dropped column reach the page as `undefined` and render as a blank cell
 * instead of a refused payload; `tests/reviews-schema.test.ts` rejects a row
 * with any one of the thirteen, the eleven or the six missing, one case per
 * field.
 *
 * Nothing here is `.strict()`. The wire may grow — 23e widened the item list
 * by six fields without touching any parser — and a page that refused a
 * payload carrying one new key would break on a deploy that changed nothing it
 * reads.
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

/** One item as 23e widened it: the eleven `ReviewItemOutput` fields. */
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
 * The item list's envelope. `review_id` rides along because the decision POST
 * is addressed by item and rendered against the review it belongs to; a page
 * that lost it would have a list it could not attribute.
 */
export const reviewItemsSchema = z.object({
  review_id: z.string(),
  items: z.array(reviewItemSchema),
});

/**
 * `decide_review_item`'s six-field answer.
 *
 * `revoked_grant_count` and `review_complete` are numbers and booleans rather
 * than anything looser: the first is what tells a reviewer whether their
 * Revoke ended a grant or found it already gone, and the second is the only
 * record that a review closed — there is no `review.completed` event and no
 * `complete_review` tool.
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
export type ReviewDecision = z.infer<typeof reviewDecisionSchema>;
