"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

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
 * `Approvals` is shown to everyone (23d decision C), and so are `Audit log`
 * (24ba decision E), `Kill switch` (24da decision D), `Rules` and `Policies`.
 * A caller holding no seat on any chain, no audit principal, or none of the
 * one `platform_admin` cell the board needs, gets the tool's `FORBIDDEN` as
 * one sentence rather than a 404; conditional nav needs the principals in the
 * shell, which no row has done.
 *
 * A client component reading `usePathname`: the current link
 * carries `aria-current="page"`, and only that one. A link is current when
 * the path equals its href or continues it past a `/`, so `/requests/abc`
 * marks My requests while `/` marks nothing (`tests/app-shell-layout.test.tsx`,
 * "marks only %s current on %s" and "marks no link current on /").
 *
 * The nav holds the ten links only. The bell and Sign out live in the
 * shell's "Account" group (`::keeps account actions out of the main nav`),
 * and the row scrolls inside itself at phone width (`::lets the nav scroll
 * inside itself at narrow widths`).
 */
const LINKS = [
  { href: "/chat", name: "Chat" },
  { href: "/requests", name: "My requests" },
  { href: "/access", name: "My access" },
  { href: "/approvals", name: "Approvals" },
  { href: "/reviews", name: "Reviews" },
  { href: "/notifications", name: "Notifications" },
  { href: "/admin/audit", name: "Audit log" },
  { href: "/admin/killswitch", name: "Kill switch" },
  { href: "/admin/rules", name: "Rules" },
  { href: "/admin/policies", name: "Policies" },
] as const;

function isCurrent(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function MainNav() {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Main"
      className="-mb-px flex items-center gap-1 overflow-x-auto whitespace-nowrap text-sm [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
    >
      {LINKS.map(({ href, name }) => {
        const current = isCurrent(pathname, href);
        return (
          <Link
            key={href}
            href={href}
            aria-current={current ? "page" : undefined}
            className={cn(
              "border-b-2 px-3 py-2.5 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              current
                ? "border-primary font-medium text-foreground"
                : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
            )}
          >
            {name}
          </Link>
        );
      })}
    </nav>
  );
}
