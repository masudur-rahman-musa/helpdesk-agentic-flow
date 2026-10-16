import { KillSwitchBoard } from "@/features/admin/killswitch/components/kill-switch-board";

/**
 * The `/admin/killswitch` route (the design, `platform_admin`). A thin server
 * wrapper, `app/(app)/admin/audit/page.tsx`'s division exactly: everything it shows
 * comes from 24cb's proxy, which the board reads for itself, so the page has
 * nothing of its own to fetch and every case renders the component directly
 * (`tests/killswitch-board.test.tsx`).
 *
 * The board alone for now. An earlier change adds the toggle — scope, action, reason
 * and the mutation — which is why this plan's whole surface is a read.
 *
 * The `QueryClientProvider` the board's hook needs is app-wide, in
 * `app/layout.tsx`.
 *
 * Reaching this page means the session cookie was present — the middleware's
 * catch-all matcher refuses it otherwise — and the board's own read
 * is what discovers a cookie whose token has since expired. A caller holding no
 * `platform_admin` principal is answered by the tool, not by this route: RBAC
 * and the kill switch live behind the MCP boundary and nowhere else, which is
 * why the nav links this page for everyone (decision D).
 */
export default function AdminKillSwitchPage() {
  return <KillSwitchBoard />;
}
