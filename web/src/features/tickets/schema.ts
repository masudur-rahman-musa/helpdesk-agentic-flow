/**
 * The wire shape of `/api/me/tickets`. Status is one of two words, so a word
 * the page has no label for fails the parse instead of drawing blank.
 */

import { z } from "zod";

export const ticketStatusSchema = z.enum(["open", "solved"]);

export const myTicketSchema = z.object({
  ticket_id: z.string(),
  // What a person sees, like TCK-0042. The id stays for keys.
  ticket_number: z.string(),
  kind: z.string(),
  title: z.string(),
  status: ticketStatusSchema,
  created_at: z.string(),
});

export const myTicketsSchema = z.object({
  tickets: z.array(myTicketSchema),
});

export type TicketStatus = z.infer<typeof ticketStatusSchema>;
export type MyTicket = z.infer<typeof myTicketSchema>;
