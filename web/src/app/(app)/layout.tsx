import { AppShell } from "@/components/layout/app-shell";

/**
 * The shell around every signed-in page. The sign-in page stays outside this
 * group and keeps its own shell with no nav.
 */
export default function AppGroupLayout({ children }: { children: React.ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
