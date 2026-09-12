import { RulesBoard } from "@/components/admin/rules-board";
import { AppShell } from "@/components/app-shell";

/**
 * The `/admin/rules` route (the design, `it_admin` and `platform_admin`). A thin
 * server wrapper, `app/admin/killswitch/page.tsx`'s division exactly:
 * everything it shows comes from 24eb's proxy, which the board reads for
 * itself, so the page has nothing of its own to fetch and every case renders
 * the component directly (`tests/rules-board.test.tsx`).
 *
 * Read-only in v1 exactly as §14 says: there is no control on this screen, and
 * the first plan that adds one adds the rule id column decision B left out.
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
export default function AdminRulesPage() {
  return (
    <AppShell>
      <RulesBoard />
    </AppShell>
  );
}
