"use client";

import Link from "next/link";

import { useUnreadCount } from "@/features/notifications/api";

/**
 * The unread count in the header, linking to `/notifications`. It has its own
 * small query, so the header never waits on the full list. No polling: it sits
 * on every page, so a timer would multiply server calls for data that changes a
 * few times a day. A failed read stays silent: an alert here would follow the
 * person onto pages that work.
 */
export function NotificationBell() {
  const unread = useUnreadCount();
  if (unread === null) {
    return null;
  }

  // Unknown while loading or after a failure, so show no number rather than a
  // false 0.
  const count = unread.count;

  return (
    <Link
      href="/notifications"
      aria-label={count === undefined ? "Notifications" : `Notifications, ${count} unread`}
      className="flex h-8 items-center gap-1 rounded-md px-2 text-foreground hover:bg-accent hover:text-accent-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {/* Decorative: the accessible name is the label above, and a glyph with a
          name of its own would be read out twice. */}
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        className="size-4"
      >
        <path d="M4 7a4 4 0 0 1 8 0v3l1.5 2h-11L4 10z" strokeLinejoin="round" />
        <path d="M6.5 12.5a1.5 1.5 0 0 0 3 0" strokeLinecap="round" />
      </svg>
      {count !== undefined && count > 0 ? (
        <span className="rounded-full bg-primary px-1.5 text-xs font-medium text-primary-foreground">
          {count}
        </span>
      ) : null}
    </Link>
  );
}
