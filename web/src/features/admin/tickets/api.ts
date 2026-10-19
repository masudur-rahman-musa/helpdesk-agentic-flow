import { useQuery } from "@tanstack/react-query";

import { type OpenTicket, openTicketsSchema } from "@/features/admin/tickets/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** `null` only while the browser follows the redirect to `/login`. */
export type OpenTicketsAnswer = OpenTicket[] | null;

/** Every open ticket in the company, oldest first. */
export function useOpenTickets() {
  return useQuery({ queryKey: queryKeys.openTickets, queryFn: fetchOpenTickets });
}

async function fetchOpenTickets(): Promise<OpenTicketsAnswer> {
  const parsed = await readWire(API.openTickets, openTicketsSchema);
  return parsed === null ? null : parsed.tickets;
}
