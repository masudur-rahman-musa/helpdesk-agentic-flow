"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useMe } from "@/features/auth/api";
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
 * The links are role-aware: `/api/me` returns the tab keys the
 * token's principals may use, computed on the server from `rbac.py`, and the
 * nav shows those. While that read is pending or has failed it shows the four
 * every principal holds and never more (decision E). Hiding is cosmetic: a
 * hidden URL still renders its page, and the tool's own 403 stays the gate.
 * `tests/role-nav.test.tsx` pins the links per profile, and
 * `tests/shell.test.tsx::MainNav — the routes it links to::links $name to
 * $href` asserts the ten by name and href for a platform_admin.
 *
 * A client component reading `usePathname`: the current link
 * carries `aria-current="page"`, and only that one. A link is current when
 * the path equals its href or continues it past a `/`, so `/requests/abc`
 * marks My requests while `/` marks nothing (`tests/app-shell-layout.test.tsx`,
 * "marks only %s current on %s" and "marks no link current on /").
 *
 * The nav holds the links only. The bell and Sign out live in the
 * shell's "Account" group (`::keeps account actions out of the main nav`),
 * and the row scrolls inside itself at phone width (`::lets the nav scroll
 * inside itself at narrow widths`).
 */
const LINKS = [
  { key: "chat", href: "/chat", name: "Chat" },
  { key: "requests", href: "/requests", name: "My requests" },
  { key: "access", href: "/access", name: "My access" },
  { key: "approvals", href: "/approvals", name: "Approvals" },
  { key: "reviews", href: "/reviews", name: "Reviews" },
  { key: "notifications", href: "/notifications", name: "Notifications" },
  { key: "audit", href: "/admin/audit", name: "Audit log" },
  { key: "killswitch", href: "/admin/killswitch", name: "Kill switch" },
  { key: "rules", href: "/admin/rules", name: "Rules" },
  { key: "policies", href: "/admin/policies", name: "Policies" },
] as const;

/** Open to every principal; what the nav shows until `/api/me` answers. */
const UNIVERSAL_KEYS: readonly string[] = ["chat", "requests", "access", "notifications"];

function isCurrent(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function MainNav() {
  const pathname = usePathname();
  const shown = useMe()?.tabs ?? UNIVERSAL_KEYS;

  return (
    <nav
      aria-label="Main"
      className="-mb-px flex items-center gap-1 overflow-x-auto whitespace-nowrap text-sm [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
    >
      {LINKS.filter(({ key }) => shown.includes(key)).map(({ href, name }) => {
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
