"use client";

import { QueryClientContext, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useContext } from "react";

import { notificationPageSchema } from "@/lib/notifications-schema";
import { readWire } from "@/lib/wire-read";

/**
 * The unread count in the shell's header, and the route into `/notifications`
 *.
 *
 * Its OWN client component with its own query (decision B), rendered in the
 * shell's "Account" group: a header on every authenticated page
 * never waits on the page's fifty rows to know whether to show a number.
 *
 * It reads `unread_only=true&limit=1` and renders `unread_count` ALONE — the
 * tool's whole tally (23ga decision B), never the length of what `limit=1`
 * handed it, which would read 1 while seven rows waited and understate every
 * count above the page size. Pinned by
 * `tests/notification-bell.test.tsx::reads unread_only=true&limit=1 and renders
 * the tally, not the rows it was handed`.
 *
 * Refetch on mount and on window focus, TanStack's own default, and NO poll
 * (decision D): the bell is on every authenticated page, so an interval
 * multiplies MCP round trips by tabs and by minutes for a table four scheduler
 * jobs write to a few times a day. Pinned as behaviour rather than as a prop by
 * `::does not poll: no second read after two minutes of clock`.
 *
 * A failed read is SILENT — the link with no count and no alert (§1). An error
 * region here sits in the header of the whole authenticated app and follows a
 * person from screen to screen, including onto the routes that are working, so
 * the one thing this component owes on failure is the route into the page.
 * Pinned by the two `a read that fails` rows.
 *
 * The read goes through `lib/wire-read.ts`, so an expired session
 * arrives as that module's `null` and never as a status (22b decision E), the
 * same as the page's two call sites. This read fires on every authenticated
 * page, so it is the likeliest place a stale session is discovered at all.
 */
export function NotificationBell() {
  // Deviation from §2, recorded in the plan: `tests/shell.test.tsx` renders
  // `AppShell` — and so this component, in its "Account" group — with no
  // `QueryClientProvider` above it, which a bare `useQuery` throws on in every
  // one of that file's cases. §8 authorises no provider wrapper there and §3
  // puts `app-shell.tsx` under "will NOT touch", so the absence of a client is
  // answered HERE, in the only file that can answer it without editing a closed
  // plan's tests. The branch is unreachable in the app:
  // `app/layout.tsx` wraps every route in `QueryProvider`, above the shell.
  const client = useContext(QueryClientContext);
  if (client === undefined) {
    return null;
  }
  return <UnreadBell />;
}

function UnreadBell() {
  const unread = useQuery({
    queryKey: ["notifications", "unread"],
    queryFn: fetchUnreadCount,
  });

  // `undefined` while the read is in flight AND after it failed, which is the
  // same thing to a reader: the count is unknown, so the bell says nothing
  // about it rather than rendering `0` and claiming nothing is waiting.
  const count = unread.data;

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
    "/api/me/notifications?unread_only=true&limit=1",
    notificationPageSchema,
  );
  return parsed === null ? 0 : parsed.unread_count;
}
