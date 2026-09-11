import { AppShell } from "@/components/app-shell";
import { ReviewQueue } from "@/components/reviews/review-queue";

/**
 * The `/reviews` route. A thin server wrapper, the same division `/requests`,
 * `/access` and `/approvals` have: everything it shows comes from the three
 * review proxies, which the client component reads for itself, so the page has
 * nothing of its own to fetch and `tests/reviews-page.test.tsx` renders the
 * queue directly.
 *
 * Reaching this page means the session cookie was present — the middleware's
 * catch-all matcher refuses it otherwise — and the queue's own read
 * is what discovers a cookie whose token has since expired. A caller who
 * reviews nothing is answered by the tool, not by this route: RBAC and the
 * scope rules live behind the MCP boundary and nowhere else.
 */
export default function ReviewsPage() {
  return (
    <AppShell>
      <ReviewQueue />
    </AppShell>
  );
}
