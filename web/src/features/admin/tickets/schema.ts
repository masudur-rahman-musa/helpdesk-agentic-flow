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

/** The answer to a solve: the ticket, and when and by whom it was first solved. */
export const solveTicketResultSchema = z.object({
  ticket_id: z.string(),
  ticket_number: z.string(),
  status: z.literal("solved"),
  solved_at: z.string(),
  solved_by_user_id: z.string(),
});

export type SolveTicketResult = z.infer<typeof solveTicketResultSchema>;
