import { AppShell } from "@/components/app-shell";
import { GrantList } from "@/components/access/grant-list";

/**
 * The `/access` route. A thin server wrapper, the same division `/chat` and
 * `/requests` have: everything it shows comes from `GET /api/me/access`, which
 * the client component reads for itself, so `tests/access-page.test.tsx`
 * renders the list directly.
 *
 * Reaching this page means the session cookie was present — the middleware's
 * catch-all matcher refuses it otherwise — and the list's own read
 * is what discovers a cookie whose token has since expired.
 */
export default function AccessPage() {
  return (
    <AppShell>
      <GrantList />
    </AppShell>
  );
}
