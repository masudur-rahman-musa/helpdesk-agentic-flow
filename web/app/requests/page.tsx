import { AppShell } from "@/components/app-shell";
import { RequestList } from "@/components/requests/request-list";

/**
 * The `/requests` route. A thin server wrapper, the same division `/chat` has:
 * everything it shows comes from `GET /api/me/requests`, which the client
 * component reads for itself, so the page has nothing of its own to fetch and
 * `tests/requests-page.test.tsx` renders the list directly.
 *
 * Reaching this page means the session cookie was present — the middleware's
 * catch-all matcher refuses it otherwise — and the list's own read
 * is what discovers a cookie whose token has since expired.
 */
export default function RequestsPage() {
  return (
    <AppShell>
      <RequestList />
    </AppShell>
  );
}
