import { useQuery } from "@tanstack/react-query";

import { type MyTicket, myTicketsSchema } from "@/features/tickets/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** This user's tickets, newest first. */
export function useMyTickets() {
  return useQuery({ queryKey: queryKeys.myTickets, queryFn: fetchTickets });
}

/**
 * This user's tickets, or an empty list while the browser follows the redirect
 * to `/login`. Errors throw to the section's own alert.
 */
async function fetchTickets(): Promise<MyTicket[]> {
  const parsed = await readWire(API.myTickets, myTicketsSchema);
  return parsed === null ? [] : parsed.tickets;
}
