import { DemoBanner } from "@/components/demo-banner";
import { MainNav } from "@/components/nav";
import { SkipLink } from "@/components/skip-link";
import { ThemeToggle } from "@/components/theme-toggle";

/**
 * Every page's frame: skip link, demo banner, header (nav plus theme toggle)
 * and the `<main id="main-content">` the skip link targets.
 *
 * The skip link is rendered FIRST so it is the first focusable element on the
 * page (`tests/shell.test.tsx`, "makes the skip link the first focusable
 * element in the shell"). `<main>` carries `tabIndex={-1}` so following the
 * link moves FOCUS there and not only the scroll position; `-1` keeps it out of
 * the tab order, which is why the test's focusable selector excludes it.
 *
 * `showNav` is off on `/login`: the nav links to authenticated routes, and a
 * sign-out button on the sign-in page is nonsense. The banner, the toggle and
 * the skip link are on every page either way, which is §1's last criterion.
 */
export function AppShell({
  children,
  showNav = true,
}: {
  children: React.ReactNode;
  showNav?: boolean;
}) {
  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <SkipLink />
      <DemoBanner />
      <header className="flex items-center justify-between gap-4 border-b border-border px-4 py-3">
        <span className="text-sm font-semibold">Helpdesk</span>
        <div className="flex items-center gap-4">
          {showNav ? <MainNav /> : null}
          <ThemeToggle />
        </div>
      </header>
      <main id="main-content" tabIndex={-1} className="flex-1 px-4 py-6 focus:outline-none">
        {children}
      </main>
    </div>
  );
}
