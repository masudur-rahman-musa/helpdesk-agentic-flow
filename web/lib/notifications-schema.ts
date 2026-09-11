/**
 * The parser for what the two `/api/...` notification proxies return (23gb).
 *
 * One file for both envelopes (decision E), the way `approvals-schema.ts` and
 * `reviews-schema.ts` carry theirs: the page and the stamp are one tool family
 * whose shapes reference each other — the stamp answers four of the eight
 * fields a row carries, and 23gc's bell replaces one row of a page it already
 * holds with what the stamp returned.
 *
 * Each schema is written over the ENVELOPE rather than over a bare row, so
 * 23gc's page runs ONE `safeParse` on the proxy's body and gets either the
 * whole payload or one refusal, never a half-parsed list it has to check again
 * per row.
 *
 * Every field is REQUIRED, and nullable exactly where the tool's own model is:
 * `subject_type` and `subject_id` (a notification about nothing in particular
 * has neither) and a row's `read_at` (an unread one has none). An `.optional()`
 * field would let a dropped column reach the page as `undefined` and render as
 * a blank cell instead of a refused payload; `tests/notifications-schema.test.ts`
 * rejects a row with any one of the eight missing, one case per field, and
 * accepts each nullable one as `null` — a nullable field and a missing one are
 * two different wire answers.
 *
 * The stamp answer's `read_at` is NOT nullable, which is the one place these
 * two envelopes disagree about one field name: the tool declares it non-null by
 * construction, because the row it answers was stamped either by that call or
 * by an earlier one (23ga decision A's already-read branch answers the STORED
 * timestamp).
 *
 * `unread_count` is a `number` and not anything looser: it is the whole reason
 * the page carries a second key at all (23ga decision B), since a bell counting
 * the rows it was handed would go quiet at the page size.
 *
 * Nothing here is `.strict()`. The wire may grow, and a page that refused a
 * payload carrying one new key would break on a deploy that changed nothing it
 * reads.
 */

import { z } from "zod";

/** One notification as its reader sees it: the eight `NotificationRow` fields. */
export const notificationRowSchema = z.object({
  notification_id: z.string(),
  kind: z.string(),
  title: z.string(),
  body: z.string(),
  subject_type: z.string().nullable(),
  subject_id: z.string().nullable(),
  created_at: z.string(),
  read_at: z.string().nullable(),
});

/** `list_notifications`' two-key envelope: the page, and the bell's own tally. */
export const notificationPageSchema = z.object({
  notifications: z.array(notificationRowSchema),
  unread_count: z.number(),
});

/**
 * `mark_notification_read`'s four-field answer.
 *
 * `read_at` is non-nullable here and nullable on a row above, deliberately:
 * this payload exists because the row it names has been read.
 */
export const notificationReadSchema = z.object({
  notification_id: z.string(),
  kind: z.string(),
  title: z.string(),
  read_at: z.string(),
});

export type NotificationRow = z.infer<typeof notificationRowSchema>;
export type NotificationPage = z.infer<typeof notificationPageSchema>;
export type NotificationRead = z.infer<typeof notificationReadSchema>;
