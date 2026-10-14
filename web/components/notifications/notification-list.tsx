"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { failureMessage } from "@/lib/chat-errors";
import { notificationKindLabel } from "@/lib/notification-labels";
import {
  type NotificationPage,
  type NotificationRow,
  notificationPageSchema,
} from "@/lib/notifications-schema";
import { readWire } from "@/lib/wire-read";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

/**
 * This person's own notifications, from `GET /api/me/notifications`, with the
 * `Unread only` filter and the per-row read stamp.
 *
 * A client component doing its own read and its own mutations through TanStack
 * Query (§2, 23d decision A), exactly `ReviewQueue`'s and `ApprovalQueue`'s
 * shape. The READ goes through `lib/wire-read.ts`, which answers
 * `null` while the browser follows the proxy's redirect to `/login` (22b
 * decision E); the stamp keeps its own check of it, because `readWire` is a GET
 * and because a notifications list sits open on a screen for many minutes
 * before anyone clicks. BOTH call sites still follow that redirect, which is
 * what `tests/notifications-page.test.tsx`'s two `an expired session` cases
 * pin, one per call site.
 *
 * Every act crosses 23gb's proxies, so a project rule holds: the stamp is a typed
 * MCP tool behind RBAC, the kill switch, the self-only scope check and the
 * audit event, and this component is the surface. Nothing here decides
 * anything — which notifications exist, whose they are, how many are unread and
 * what a second stamp does to an already-read row (23ga decision A) all come
 * from the tools; this renders and labels.
 *
 * The list paints UNFILTERED first (§1): the filtered view hides every row the
 * moment it is stamped, so a page that opened filtered would take "everything I
 * have" away from anyone who did not know the control was there. Pinned by
 * `::paints the unfiltered list first`.
 *
 * A failed read THROWS rather than answering an empty list, so the alert and the
 * empty state are mutually exclusive by construction: `You have no
 * notifications.` over a broken proxy is the sentence a person acts on by
 * closing the tab, believing an expiring grant was never announced. Pinned from
 * both sides by `::renders the named empty state with no list and no alert` and
 * the two `renders the page alert` rows.
 *
 * Accessible names: the region is `Notifications`, the rows are the list `Your
 * notifications`, the filter is the checkbox `Unread only`, and each unread
 * row's control carries the title (`Mark read {title}`) because fifty rows
 * otherwise carry fifty controls with one name.
 *
 * Dates render as the ISO prefix, never `toLocaleDateString`, which answers a
 * different day either side of a date line and is asserted absent by
 * `::renders one row per notification...`.
 */

/**
 * The `VALIDATION` sentence on this surface, and only on this surface
 * (decision G).
 *
 * `FAILURE_MESSAGES.VALIDATION` is "That message could not be sent.", which is
 * about a chat message and says nothing true about a refused stamp; 22b's
 * `tests/chat-schema.test.ts` pins every string in that map verbatim, so
 * widening the shared map is not available. Exactly ONE code is overridden —
 * every other sentence still comes from the shared map, so no second opinion
 * about `FORBIDDEN` or `TOOL_DISABLED` can appear here. Pinned by
 * `tests/notifications-page.test.tsx::pins the VALIDATION override sentence
 * exactly`.
 */
export const NOTIFICATION_VALIDATION_MESSAGE =
  "That notification could not be marked read. Reload the page and try again.";

/**
 * The prefix both notification queries share (decision C), so one stamp moves
 * the list AND the bell's count.
 *
 * The page's key carries its filter, the bell's carries `unread`, and the stamp
 * invalidates this PREFIX rather than either key: invalidating the page's own
 * key alone would leave the bell in the header of every screen counting the row
 * the person just cleared, and only a reload would fix it. Pinned by
 * `tests/notifications-page.test.tsx::refetches both the list and the bell
 * through the notifications prefix`.
 */
export const NOTIFICATIONS_KEY_PREFIX = ["notifications"] as const;

export function NotificationList() {
  // Off on first paint (§1): a person's first view of the route is everything
  // they have, and the unread view is one click away.
  const [unreadOnly, setUnreadOnly] = useState(false);

  const notifications = useQuery({
    queryKey: [...NOTIFICATIONS_KEY_PREFIX, "page", unreadOnly],
    queryFn: () => fetchNotifications(unreadOnly),
  });

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
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          Your notifications could not be read. Try again in a moment.
        </p>
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
 * One notification, and its stamp while it is still unread.
 *
 * The mutation lives per ROW on purpose. One in-flight flag for the whole list
 * would grey out every control while a single row is being stamped, which a
 * person clearing a page of fifty pays for fifty times; the refusal is scoped
 * the same way, because one banner for the whole list leaves nobody able to say
 * which of fifty rows was refused. Pinned by `::disables the stamping row's
 * control while it is in flight, and no other row's` and the four refusal rows.
 *
 * A row the tool has already stamped carries NO control: the second stamp
 * answers the stored timestamp (23ga decision A), so what a person would get
 * for clicking is a no-op on a row that is already read, and the bell's count
 * would not move.
 *
 * Nothing is stamped optimistically — the returned `read_at` is never written
 * into the list by hand, so the date on screen is only ever what a read
 * answered. `::refetches both the list and the bell through the notifications
 * prefix` pins that from the other side: the stamp answers one timestamp and
 * the refetch another, and only the refetch's is rendered.
 */
function NotificationItem({ row }: { row: NotificationRow }) {
  const queryClient = useQueryClient();

  const stamp = useMutation({
    mutationFn: () => postRead(row.notification_id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_KEY_PREFIX });
    },
  });

  const refusal = stampRefusal(stamp.error);

  return (
    <Card as="li" className="flex flex-col gap-2 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{notificationKindLabel(row.kind)}</span>
        <span>{row.title}</span>
      </div>

      <p className="text-muted-foreground">{row.body}</p>

      <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
        <span>{row.created_at.slice(0, 10)}</span>
        {row.read_at === null ? (
          <Badge tone="info">Unread</Badge>
        ) : (
          <Badge tone="neutral">Read {row.read_at.slice(0, 10)}</Badge>
        )}
      </div>

      {refusal !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2">
          {refusal}
        </p>
      ) : null}

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

/**
 * A stamp the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
class StampRefusedError extends Error {
  readonly code: unknown;

  constructor(code: unknown) {
    super("the read stamp was refused");
    this.code = code;
  }
}

/** The sentence for a refused stamp, or `null` while nothing is refused. */
function stampRefusal(error: unknown): string | null {
  if (error === null || error === undefined) {
    return null;
  }
  const code = error instanceof StampRefusedError ? error.code : undefined;
  return code === "VALIDATION" ? NOTIFICATION_VALIDATION_MESSAGE : failureMessage(code);
}

/**
 * One page of notifications, filtered or not.
 *
 * `unread_only` is sent only when the control is ON (decision E): the tool
 * takes a bool, and `unread_only=false` or a bare `unread_only=` is a value it
 * has no reading for. No `limit` rides along — nothing on this surface pages
 * yet, so the tool's own default is what the list shows, and the absent key is
 * also what tells this read apart from the bell's in
 * `tests/notifications-page.test.tsx`.
 */
async function fetchNotifications(unreadOnly: boolean): Promise<NotificationPage> {
  const url = unreadOnly ? "/api/me/notifications?unread_only=true" : "/api/me/notifications";
  // The empty page is what the redirect resolves to, exactly as before: no row,
  // no tally, and nothing said about either while the browser leaves. A refusal
  // and a failed read both throw out of `readWire` instead, and this page
  // dresses neither (24ia decision B) — the list's own alert answers a typed 403
  // and a bodyless 500 alike, and the per-code sentences here stay the STAMP's
  // (`tests/notifications-page.test.tsx::a 403 FORBIDDEN on the page read
  // renders the page alert, not the code's sentence`).
  const parsed = await readWire(url, notificationPageSchema);
  return parsed === null ? { notifications: [], unread_count: 0 } : parsed;
}

/**
 * One read stamp, posted to the proxy for this row.
 *
 * No body and no content type (23gb decision B): the id is in the path and
 * there is nothing else for the browser to say. No idempotency key either —
 * agent-api derives it from the verified reader and the notification, so a
 * double-clicked control replays the stored response instead of filing a second
 * event, and a key this page chose could collide across two notifications.
 *
 * The id comes off the PARSED payload and goes through `encodeURIComponent`:
 * `notification_id` is a bare `z.string()` on the wire, so a value carrying a
 * path separator would otherwise reach a different route entirely.
 */
async function postRead(notificationId: string): Promise<void> {
  const response = await fetch(`/api/notifications/${encodeURIComponent(notificationId)}/read`, {
    method: "POST",
  });

  if (response.redirected) {
    window.location.assign(response.url);
    return;
  }
  if (!response.ok) {
    const refused: unknown = await response.json().catch(() => null);
    const code =
      typeof refused === "object" && refused !== null
        ? (refused as Record<string, unknown>).code
        : undefined;
    throw new StampRefusedError(code);
  }
}
