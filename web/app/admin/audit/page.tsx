import { AuditSearch } from "@/components/admin/audit-search";
import { AuditVerify } from "@/components/admin/audit-verify";
import { AppShell } from "@/components/app-shell";

/**
 * The `/admin/audit` route. A thin server wrapper, the same division `/requests`,
 * `/access`, `/approvals` and `/reviews` have: everything it shows comes from
 * 24a's two proxies, which the client components read for themselves, so the
 * page has nothing of its own to fetch and every case renders one of them
 * directly (`tests/audit-search-page.test.tsx`, `tests/audit-export.test.tsx`,
 * `tests/audit-verify-panel.test.tsx`).
 *
 * Two regions side by side: 24ba's `Audit log` with its
 * `Export CSV`, and the `Chain integrity` check. They share no state — no
 * filters, no cursor, no page size — which is why the verification is its own
 * component rather than a control inside the search. The `QueryClientProvider`
 * both of their hooks need is app-wide, in `app/layout.tsx`.
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
      <div className="flex flex-col gap-8">
        <AuditSearch />
        <AuditVerify />
      </div>
    </AppShell>
  );
}
