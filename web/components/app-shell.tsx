import { DemoBanner } from "@/components/demo-banner";
import { MainNav } from "@/components/nav";
import { NotificationBell } from "@/components/notifications/notification-bell";
import { SkipLink } from "@/components/skip-link";
import { ThemeToggle } from "@/components/theme-toggle";
import { Container } from "@/components/ui/container";
import { UserMenu } from "@/components/user-menu";

/**
 * Every page's frame: skip link, demo banner, a two-row header and the
 * `<main id="main-content">` the skip link targets.
 *
 * The skip link is rendered FIRST so it is the first focusable element on the
 * page (`tests/shell.test.tsx`, "makes the skip link the first focusable
 * element in the shell"). `<main>` carries `tabIndex={-1}` so following the
 * link moves FOCUS there and not only the scroll position; `-1` keeps it out of
 * the tab order, which is why the test's focusable selector excludes it.
 *
 * The header: the brand on the left and the "Account"
 * group on the right of the top row, holding the bell, the theme toggle and
 * the user menu in that order; `MainNav` alone on the row below, scrolling inside
 * itself at phone width. Both rows and the page content sit in one
 * `Container`, so their edges line up (`tests/app-shell-layout.test.tsx`,
 * "groups the account actions on the right" and "renders page content inside
 * the max-w-6xl container").
 *
 * `UserMenu` is the signed-in person's name and account type as one
 * button that opens Sign out. Until `/api/me` answers it renders the plain
 * Sign out button, so signing out never waits on that read (decision C).
 *
 * `showNav` is off on `/login`: the nav links to authenticated routes, and a
 * sign-out button or an unread count on the sign-in page is nonsense. The
 * banner, the toggle and the skip link are on every page either way
 * (`::shows only the theme toggle on the sign-in shell`).
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
      <header className="border-b border-border bg-card">
        <Container>
          <div className="flex h-14 items-center justify-between gap-4">
            <span className="flex items-center gap-2 text-sm font-semibold tracking-tight">
              <span
                aria-hidden="true"
                className="flex size-6 items-center justify-center rounded-md bg-primary text-xs font-bold text-primary-foreground"
              >
                H
              </span>
              Helpdesk
            </span>
            <div role="group" aria-label="Account" className="flex items-center gap-2">
              {showNav ? <NotificationBell /> : null}
              <ThemeToggle />
              {showNav ? <UserMenu /> : null}
            </div>
          </div>
          {showNav ? <MainNav /> : null}
        </Container>
      </header>
      <main id="main-content" tabIndex={-1} className="flex-1 py-6 focus:outline-none">
        <Container>{children}</Container>
      </main>
    </div>
  );
}
