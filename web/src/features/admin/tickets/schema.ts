/**
 * The wire shape of `/api/admin/tickets`. No status: every row is open. Every
 * field is required, so a dropped column fails the parse instead of drawing blank.
 */

import { z } from "zod";

export const openTicketSchema = z.object({
  ticket_id: z.string(),
  // What a person sees, like TCK-0042. The id stays for keys.
  ticket_number: z.string(),
  kind: z.string(),
  title: z.string(),
  opened_by: z.string(),
  created_at: z.string(),
});

export const openTicketsSchema = z.object({
  tickets: z.array(openTicketSchema),
});

export type OpenTicket = z.infer<typeof openTicketSchema>;
