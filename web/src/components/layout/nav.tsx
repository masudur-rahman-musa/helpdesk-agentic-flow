"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useMe } from "@/features/auth/api";
import { cn } from "@/lib/utils";

/**
 * Main nav. It shows the tabs `/api/me` says the user's roles may use, and only
 * the four everyone holds until that answers. Hiding is cosmetic: the server's
 * 403 is the real check. A link is current on its href or any path below it.
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
  { key: "settings", href: "/admin/settings", name: "Settings" },
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
