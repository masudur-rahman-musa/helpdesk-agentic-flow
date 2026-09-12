import Link from "next/link";

import { NotificationBell } from "@/components/notifications/notification-bell";
import { SignOutButton } from "@/components/sign-out-button";

/**
 * The shell's navigation. It links only to routes that exist: `/requests` and
 * `/access` are pages as of an earlier change, `/approvals` as of an earlier change, `/reviews`
 * as of an earlier change, `/notifications` as of an earlier change, `/admin/audit` as of
 * an earlier change, `/admin/killswitch` as of an earlier change and `/admin/rules` as of
 * an earlier change, while `/admin/policies` arrives later in row 24, and a link to a
 * 404 is worse than no link — the user pays the 404 to find out.
 * `tests/shell.test.tsx` asserts both halves: the nine links by name and href,
 * and the absence of a link to each route nothing serves yet.
 *
 * The unread count is `NotificationBell`, a separate client component with its
 * own query (23gc decision B), so this file stays hook-free.
 *
 * `Approvals` is shown to everyone (23d decision C), and so are `Audit log`
 * (24ba decision E), `Kill switch` (24da decision D) and `Rules`. A caller
 * holding no
 * seat on any chain, no audit principal, or none of the one `platform_admin`
 * cell the board needs, gets the tool's `FORBIDDEN` as one sentence rather
 * than a 404;
 * conditional nav needs the principals in the shell, which no row has done.
 *
 * A plain component on purpose — no `usePathname`, so the shell renders in a
 * test without a router context and `tests/shell.test.tsx` needs no mock.
 */
export function MainNav() {
  return (
    <nav aria-label="Main" className="flex items-center gap-4 text-sm">
      <Link
        href="/chat"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Chat
      </Link>
      <Link
        href="/requests"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        My requests
      </Link>
      <Link
        href="/access"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        My access
      </Link>
      <Link
        href="/approvals"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Approvals
      </Link>
      <Link
        href="/reviews"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Reviews
      </Link>
      <Link
        href="/notifications"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Notifications
      </Link>
      <Link
        href="/admin/audit"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Audit log
      </Link>
      <Link
        href="/admin/killswitch"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Kill switch
      </Link>
      <Link
        href="/admin/rules"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Rules
      </Link>
      <NotificationBell />
      <SignOutButton />
    </nav>
  );
}
