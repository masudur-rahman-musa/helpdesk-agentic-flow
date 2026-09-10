import Link from "next/link";

import { SignOutButton } from "@/components/sign-out-button";

/**
 * The shell's navigation. It links only to routes that exist: the per-principal
 * routes (requests, access, approvals, reviews, admin, notifications) arrive
 * with rows 23 and 24, and a link to a 404 is worse than no link.
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
      <SignOutButton />
    </nav>
  );
}
