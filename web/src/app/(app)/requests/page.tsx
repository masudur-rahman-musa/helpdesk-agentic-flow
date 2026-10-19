import { RequestList } from "@/features/requests/components/request-list";
import { TicketList } from "@/features/tickets/components/ticket-list";

/**
 * The `/requests` route. A thin server wrapper; each list fetches its own data.
 * Tickets sit below requests instead of on their own tab.
 */
export default function RequestsPage() {
  return (
    <div className="flex flex-col gap-8">
      <RequestList />
      <TicketList />
    </div>
  );
}
