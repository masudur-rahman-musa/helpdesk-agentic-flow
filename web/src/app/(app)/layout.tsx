import { AppShell } from "@/components/layout/app-shell";

/**
 * The one layout for every signed-in page. The `(app)` route
 * group holds `/chat`, `/access`, `/requests`, `/approvals`, `/reviews`,
 * `/notifications` and the four `/admin/*` screens; the parentheses mean the
 * group name is not in any URL. This layout renders the shell once around all
 * of them, so no page wraps itself in `<AppShell>` any more.
 *
 * The sign-in page is not signed in, so it stays outside this group and keeps
 * its own shell with the nav turned off.
 */
export default function AppGroupLayout({ children }: { children: React.ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
