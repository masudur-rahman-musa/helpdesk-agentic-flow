/**
 * Parsers for the notification page and the read stamp. Every field is
 * required, so a dropped column is refused instead of showing as a blank cell.
 * Nothing is `.strict()`, so a new server key does not break the page.
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

/** `mark_notification_read`'s answer. `read_at` is never null: the row was read. */
export const notificationReadSchema = z.object({
  notification_id: z.string(),
  kind: z.string(),
  title: z.string(),
  read_at: z.string(),
});

export type NotificationRow = z.infer<typeof notificationRowSchema>;
export type NotificationPage = z.infer<typeof notificationPageSchema>;
export type NotificationRead = z.infer<typeof notificationReadSchema>;
