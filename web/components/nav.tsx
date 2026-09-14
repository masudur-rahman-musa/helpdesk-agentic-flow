import Link from "next/link";

import { NotificationBell } from "@/components/notifications/notification-bell";
import { SignOutButton } from "@/components/sign-out-button";

/**
 * The shell's navigation. It links only to routes that exist: `/requests` and
 * `/access` are pages as of an earlier change, `/approvals` as of an earlier change, `/reviews`
 * as of an earlier change, `/notifications` as of an earlier change, `/admin/audit` as of
 * an earlier change, `/admin/killswitch` as of an earlier change, `/admin/rules` as of
 * an earlier change and `/admin/policies` as of an earlier change, which is every route in
 * the design's table: nothing is left for the nav to withhold, so the rule that
 * a link to a 404 is worse than no link has nothing to bite on here any more.
 * `tests/shell.test.tsx::MainNav — the routes it links to::links $name to
 * $href` asserts the ten links by name and href; its companion case naming the
 * routes nothing served yet went with the last of them.
 *
 * The unread count is `NotificationBell`, a separate client component with its
 * own query (23gc decision B), so this file stays hook-free.
 *
 * `Approvals` is shown to everyone (23d decision C), and so are `Audit log`
 * (24ba decision E), `Kill switch` (24da decision D), `Rules` and `Policies`.
 * A caller holding no seat on any chain, no audit principal, or none of the
 * one `platform_admin` cell the board needs, gets the tool's `FORBIDDEN` as
 * one sentence rather than a 404; conditional nav needs the principals in the
 * shell, which no row has done.
 *
 * No `usePathname` here: the links are fixed and a nav that read the route
 * would re-render the header on every navigation for nothing. That is NOT the
 * same as being router-free, and this file said so until an earlier change —
 * `SignOutButton` below calls `useRouter`, which throws outside a Next tree, so
 * `tests/shell.test.tsx` mocks `next/navigation`.
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
      <Link
        href="/admin/policies"
        className="rounded-sm text-foreground hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Policies
      </Link>
      <NotificationBell />
      <SignOutButton />
    </nav>
  );
}
