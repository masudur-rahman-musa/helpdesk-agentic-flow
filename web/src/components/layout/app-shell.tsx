import { DemoBanner } from "@/components/layout/demo-banner";
import { MainNav } from "@/components/layout/nav";
import { NotificationBell } from "@/features/notifications/components/notification-bell";
import { SkipLink } from "@/components/layout/skip-link";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { Container } from "@/components/ui/container";
import { UserMenu } from "@/components/layout/user-menu";

/**
 * Every page's frame. The skip link comes first so it is the first focusable
 * element; `<main>` has `tabIndex={-1}` so the link moves focus, not just the
 * scroll. `showNav` is off on `/login`, where nav links and sign-out make no sense.
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
