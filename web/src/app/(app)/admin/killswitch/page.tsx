import { KillSwitchBoard } from "@/features/admin/killswitch/components/kill-switch-board";

/**
 * The `/admin/killswitch` route. A thin server wrapper; the board fetches its
 * own data. Only `platform_admin` gets an answer, and that is checked by the
 * tool behind the MCP server, not here.
 */
export default function AdminKillSwitchPage() {
  return <KillSwitchBoard />;
}
