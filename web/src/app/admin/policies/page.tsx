import { PoliciesBoard } from "@/features/admin/policies/components/policies-board";
import { AppShell } from "@/components/layout/app-shell";

/**
 * The `/admin/policies` route (the design, `it_admin` and `platform_admin`). A
 * thin server wrapper, `app/admin/rules/page.tsx`'s division exactly:
 * everything it shows comes from 24g's proxy, which the board reads for itself,
 * so the page has nothing of its own to fetch and every case renders the
 * component directly (`tests/policies-board.test.tsx`).
 *
 * Read-only in v1 exactly as §14 says: there is no control on this screen. The
 * re-ingest the same cell names is row 31's, and it owns whatever control this
 * page grows to hold it.
 *
 * The `QueryClientProvider` the board's hook needs is app-wide, in
 * `app/layout.tsx`.
 *
 * Reaching this page means the session cookie was present — the middleware's
 * catch-all matcher refuses it otherwise — and the board's own read
 * is what discovers a cookie whose token has since expired. A caller holding
 * neither `it_admin` nor `platform_admin` is answered by the tool, not by this
 * route: RBAC lives behind the MCP boundary and nowhere else, which is why the
 * nav links this page for everyone.
 */
export default function AdminPoliciesPage() {
  return (
    <AppShell>
      <PoliciesBoard />
    </AppShell>
  );
}
