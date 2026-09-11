import Link from "next/link";

import { SignOutButton } from "@/components/sign-out-button";

/**
 * The shell's navigation. It links only to routes that exist: `/requests` and
 * `/access` are pages as of an earlier change, while approvals, reviews, admin and
 * notifications arrive with rows 23c to 24, and a link to a 404 is worse than
 * no link — the user pays the 404 to find out. `tests/shell.test.tsx` asserts
 * both halves: the three links by name and href, and the absence of a link to
 * each route nothing serves yet.
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
      <SignOutButton />
    </nav>
  );
}
