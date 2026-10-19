"use client";

import type { MyTicket } from "@/features/tickets/schema";
import { useMyTickets } from "@/features/tickets/api";
import { ticketKindLabel, ticketStatusLabel } from "@/features/tickets/labels";
import { StatusBadge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Alert } from "@/components/ui/alert";

/**
 * The tickets this user opened, newest first. A failed read shows an alert,
 * never "you have not opened any tickets", like `RequestList`.
 */
export function TicketList() {
  const tickets = useMyTickets();

  return (
    <section aria-label="My tickets" className="flex max-w-3xl flex-col gap-4">
      <PageHeader title="My tickets" />

      {tickets.isError ? (
        <Alert>Your tickets could not be read. Try again in a moment.</Alert>
      ) : null}

      {tickets.isSuccess && tickets.data.length === 0 ? (
        <EmptyState title="You have not opened any tickets." />
      ) : null}

      {tickets.isSuccess && tickets.data.length > 0 ? (
        <ul aria-label="Tickets" className="flex flex-col gap-3">
          {tickets.data.map((ticket) => (
            <TicketRow key={ticket.ticket_id} ticket={ticket} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function TicketRow({ ticket }: { ticket: MyTicket }) {
  return (
    <Card as="li" className="flex flex-wrap items-center gap-2 p-4 text-sm">
      <span className="text-muted-foreground tabular-nums">{ticket.ticket_number}</span>
      <span className="font-medium">{ticket.title}</span>
      <span className="text-muted-foreground">{ticketKindLabel(ticket.kind)}</span>
      <StatusBadge status={ticket.status}>{ticketStatusLabel(ticket.status)}</StatusBadge>
    </Card>
  );
}
