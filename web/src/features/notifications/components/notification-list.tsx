"use client";

import { useState } from "react";

import { failureMessage } from "@/lib/failure-messages";
import { notificationKindLabel } from "@/features/notifications/labels";
import { StampRefusedError, useMarkRead, useNotificationPage } from "@/features/notifications/api";
import type { NotificationRow } from "@/features/notifications/schema";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Alert } from "@/components/ui/alert";
import { formatIsoDate } from "@/lib/date-format";

/**
 * This person's notifications, with an `Unread only` filter and a read stamp
 * per row. Every action goes through a typed server tool; this only renders.
 * It opens unfiltered, so stamped rows do not vanish for someone who never saw
 * the filter. A failed read shows an alert, never the empty state, so nobody
 * thinks an expiring grant was never announced.
 */

/**
 * The shared `VALIDATION` sentence is about chat messages, so this page swaps in
 * its own for that one code only.
 */
export const NOTIFICATION_VALIDATION_MESSAGE =
  "That notification could not be marked read. Reload the page and try again.";

export function NotificationList() {
  const [unreadOnly, setUnreadOnly] = useState(false);

  const notifications = useNotificationPage(unreadOnly);

  const rows = notifications.data?.notifications ?? [];

  return (
    <section aria-label="Notifications" className="flex max-w-3xl flex-col gap-4">
      <PageHeader title="Notifications" />

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          aria-label="Unread only"
          checked={unreadOnly}
          onChange={(event) => setUnreadOnly(event.target.checked)}
          className="size-4 rounded-sm border border-border focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        Unread only
      </label>

      {notifications.isError ? (
        <Alert>Your notifications could not be read. Try again in a moment.</Alert>
      ) : null}

      {notifications.isSuccess && rows.length === 0 ? (
        <EmptyState title="You have no notifications." />
      ) : null}

      {notifications.isSuccess && rows.length > 0 ? (
        <ul aria-label="Your notifications" className="flex flex-col gap-3">
          {rows.map((row) => (
            <NotificationItem key={row.notification_id} row={row} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

/**
 * One notification, and its stamp while unread. Each row has its own mutation,
 * so stamping one row does not lock the others and a refusal shows on the right
 * row. Nothing is updated by hand: the date shown is always what a read
 * returned.
 */
function NotificationItem({ row }: { row: NotificationRow }) {
  const stamp = useMarkRead(row.notification_id);

  const refusal = stampRefusal(stamp.error);

  return (
    <Card as="li" className="flex flex-col gap-2 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{notificationKindLabel(row.kind)}</span>
        <span>{row.title}</span>
      </div>

      <p className="text-muted-foreground">{row.body}</p>

      <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
        <span>{formatIsoDate(row.created_at)}</span>
        {row.read_at === null ? (
          <Badge tone="info">Unread</Badge>
        ) : (
          <Badge tone="neutral">Read {formatIsoDate(row.read_at)}</Badge>
        )}
      </div>

      {refusal !== null ? <Alert>{refusal}</Alert> : null}

      {row.read_at === null ? (
        <div>
          <Button
            type="button"
            aria-label={`Mark read ${row.title}`}
            disabled={stamp.isPending}
            onClick={() => stamp.mutate()}
          >
            Mark read
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

/** The sentence for a refused stamp, or `null` while nothing is refused. */
function stampRefusal(error: unknown): string | null {
  if (error === null || error === undefined) {
    return null;
  }
  const code = error instanceof StampRefusedError ? error.code : undefined;
  return code === "VALIDATION" ? NOTIFICATION_VALIDATION_MESSAGE : failureMessage(code);
}
