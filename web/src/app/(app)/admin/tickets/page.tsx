import { OpenTicketsBoard } from "@/features/admin/tickets/components/open-tickets-board";

/**
 * The `/admin/tickets` route. A thin, read-only server wrapper; the board
 * fetches its own data. Access is checked by the tool behind the MCP server,
 * not here.
 */
export default function AdminTicketsPage() {
  return <OpenTicketsBoard />;
}
