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
 * Mark one notification read. It refreshes the whole notifications prefix, so
 * the list and the header bell's count both move after one click.
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
 * The bell's unread count, `undefined` while loading or after a failure.
 * `null` when no query client is above, as in a bare shell render; the app
 * always has one.
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
 * The unread tally with `limit=1`: the count is the full total, so one row
 * says as much as fifty.
 */
async function fetchUnreadCount(): Promise<number> {
  // A failure throws instead of returning 0, because 0 claims nothing is
  // waiting. A redirect to `/login` returns 0, which shows no badge.
  const parsed = await readWire(
    `${API.myNotifications}?unread_only=true&limit=1`,
    notificationPageSchema,
  );
  return parsed === null ? 0 : parsed.unread_count;
}

/**
 * A refused read stamp. It carries only the typed code; the app picks the
 * sentence.
 */
export class StampRefusedError extends Error {
  readonly code: unknown;

  constructor(code: unknown) {
    super("the read stamp was refused");
    this.code = code;
  }
}

/**
 * One page of notifications. `unread_only` is sent only when on, since the
 * tool takes a bool and has no reading for an empty value.
 */
async function fetchNotifications(unreadOnly: boolean): Promise<NotificationPage> {
  const url = unreadOnly ? `${API.myNotifications}?unread_only=true` : API.myNotifications;
  // A redirect to `/login` gives an empty page. Errors throw to the list's own
  // alert.
  const parsed = await readWire(url, notificationPageSchema);
  return parsed === null ? { notifications: [], unread_count: 0 } : parsed;
}

/**
 * One read stamp. No idempotency key: the server makes it from the reader and
 * the notification, so a double click replays. The id is URL-encoded so it
 * cannot reach a different route.
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
