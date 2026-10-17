import { RulesBoard } from "@/features/admin/rules/components/rules-board";

/**
 * The `/admin/rules` route. A thin, read-only server wrapper; the board fetches
 * its own data. Access is checked by the tool behind the MCP server, not here,
 * so the nav shows this page to everyone.
 */
export default function AdminRulesPage() {
  return <RulesBoard />;
}
