import { QueryClientContext, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useContext } from "react";

import { type NotificationPage, notificationPageSchema } from "@/features/notifications/schema";
import { API, apiPath } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** One page of notifications, filtered to unread or not. */
export function useNotificationPage(unreadOnly: boolean) {
  return useQuery({
    queryKey: queryKeys.notificationPage(unreadOnly),
    queryFn: () => fetchNotifications(unreadOnly),
  });
}

/**
 * Mark one notification read. A success invalidates the prefix both
 * notification queries share (decision C), so one stamp moves the list AND the
 * bell's count.
 *
 * The page's key carries its filter, the bell's carries `unread`, and the stamp
 * invalidates this PREFIX rather than either key: invalidating the page's own
 * key alone would leave the bell in the header of every screen counting the row
 * the person just cleared, and only a reload would fix it. Pinned by
 * `tests/notifications-page.test.tsx::refetches both the list and the bell
 * through the notifications prefix`.
 */
export function useMarkRead(notificationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => postRead(notificationId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.notifications });
    },
  });
}

/**
 * The bell's unread count: `{ count }`, where `count` is `undefined` while the
 * read is in flight and after it failed, or `null` when no
 * `QueryClientProvider` is above.
 *
 * The `null` exists for `tests/shell.test.tsx`, which renders `AppShell` (and
 * so the bell) with no provider, where a bare `useQuery` throws. §8 of plan
 * 23gc authorised no provider wrapper there, so the absence of a client is
 * answered here rather than by editing a closed plan's tests.
 * The branch is unreachable in the app: `app/layout.tsx` wraps every route in
 * `QueryProvider`, above the shell.
 *
 * Refetch on mount and on window focus, TanStack's own default, and NO poll
 * (23gc decision D).
 */
export function useUnreadCount(): { count: number | undefined } | null {
  const client = useContext(QueryClientContext);
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return client === undefined ? null : { count: useUnreadQuery() };
}

function useUnreadQuery(): number | undefined {
  const unread = useQuery({ queryKey: queryKeys.unreadCount, queryFn: fetchUnreadCount });
  return unread.data;
}

/**
 * The tally, from the smallest payload that carries it.
 *
 * `limit=1` because `unread_count` is the whole count and not the length of the
 * list (23ga decision B), so one row is as informative as fifty and the header
 * of every page should not be fetching rows it renders none of. The absent
 * `limit` on the page's own read is what tells the two apart in the tests.
 */
async function fetchUnreadCount(): Promise<number> {
  // `readWire` THROWS for every failure, even though the failure renders as
  // nothing here: a resolved `0` is a claim that nothing is waiting, and the
  // difference between that and "unknown" is the whole of §1's silent-failure
  // criterion (`tests/notification-bell.test.tsx::a 403 FORBIDDEN leaves the
  // count unknown, never 0, and shows no alert`). Its `null` — the browser
  // already navigating to `/login` — is the one case that resolves, and `0` is
  // then never rendered either, because a count of zero shows no badge.
  const parsed = await readWire(
    `${API.myNotifications}?unread_only=true&limit=1`,
    notificationPageSchema,
  );
  return parsed === null ? 0 : parsed.unread_count;
}

/**
 * A stamp the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
export class StampRefusedError extends Error {
  readonly code: unknown;

  constructor(code: unknown) {
    super("the read stamp was refused");
    this.code = code;
  }
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
  const url = unreadOnly ? `${API.myNotifications}?unread_only=true` : API.myNotifications;
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
  const response = await fetch(apiPath(API.readNotification, { notificationId }), {
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
