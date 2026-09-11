import { AppShell } from "@/components/app-shell";
import { ApprovalQueue } from "@/components/approvals/approval-queue";

/**
 * The `/approvals` route. A thin server wrapper, the same division `/requests`
 * and `/access` have: everything it shows comes from `GET /api/me/approvals`,
 * which the client component reads for itself, so the page has nothing of its
 * own to fetch and `tests/approvals-page.test.tsx` renders the queue directly.
 *
 * Reaching this page means the session cookie was present — the middleware's
 * catch-all matcher refuses it otherwise — and the queue's own read
 * is what discovers a cookie whose token has since expired. A caller holding no
 * seat on any chain is refused by the tool, not by this route: RBAC lives
 * behind the MCP boundary and nowhere else.
 */
export default function ApprovalsPage() {
  return (
    <AppShell>
      <ApprovalQueue />
    </AppShell>
  );
}
