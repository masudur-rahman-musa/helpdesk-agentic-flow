import { AuditSearch } from "@/components/admin/audit-search";
import { AppShell } from "@/components/app-shell";

/**
 * The `/admin/audit` route. A thin server wrapper, the same division `/requests`,
 * `/access`, `/approvals` and `/reviews` have: everything it shows comes from
 * 24a's search proxy, which the client component reads for itself, so the page
 * has nothing of its own to fetch and `tests/audit-search-page.test.tsx` renders
 * the search directly.
 *
 * Reaching this page means the session cookie was present — the middleware's
 * catch-all matcher refuses it otherwise — and the search's own read
 * is what discovers a cookie whose token has since expired. A caller holding no
 * audit principal is answered by the tool, not by this route: RBAC and the kill
 * switch live behind the MCP boundary and nowhere else, which is why the nav
 * links this page for everyone (decision E).
 */
export default function AdminAuditPage() {
  return (
    <AppShell>
      <AuditSearch />
    </AppShell>
  );
}
