"use client";

import Link from "next/link";

import { useMyRequests } from "@/features/requests/api";
import { useMyTickets } from "@/features/tickets/api";
import { ticketStatusLabel } from "@/features/tickets/labels";
import { StatusBadge } from "@/components/ui/badge";
import { Alert } from "@/components/ui/alert";
import { statusLabel } from "@/lib/decision-labels";

/**
 * The person's own access requests and tickets, beside the chat. Only the two
 * own-list reads: never the approvals queue, which is what others asked of
 * them (`my-requests-panel.test.tsx::never calls the approvals route`). Each
 * list fails on its own, so a broken read never blanks the other.
 */
export function MyRequestsPanel() {
  const requests = useMyRequests();
  const tickets = useMyTickets();

  return (
    <section aria-label="My requests" className="flex flex-col gap-3 text-sm">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="font-semibold">My requests</h2>
        <Link href="/requests" className="text-muted-foreground underline-offset-4 hover:underline">
          See all
        </Link>
      </div>

      {requests.isError ? <Alert>Your requests could not be read.</Alert> : null}
      {requests.isSuccess && requests.data.length === 0 ? (
        <p className="text-muted-foreground">No access requests yet.</p>
      ) : null}
      {requests.isSuccess && requests.data.length > 0 ? (
        <ul aria-label="Access requests" className="flex flex-col gap-2">
          {requests.data.map((request) => (
            <li key={request.request_id} className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-muted-foreground tabular-nums">{request.request_number}</span>
              <span className="font-medium">{request.role_name}</span>
              <StatusBadge status={request.status}>{statusLabel(request.status)}</StatusBadge>
            </li>
          ))}
        </ul>
      ) : null}

      <h2 className="pt-2 font-semibold">My tickets</h2>
      {tickets.isError ? <Alert>Your tickets could not be read.</Alert> : null}
      {tickets.isSuccess && tickets.data.length === 0 ? (
        <p className="text-muted-foreground">No tickets yet.</p>
      ) : null}
      {tickets.isSuccess && tickets.data.length > 0 ? (
        <ul aria-label="Tickets" className="flex flex-col gap-2">
          {tickets.data.map((ticket) => (
            <li key={ticket.ticket_id} className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-muted-foreground tabular-nums">{ticket.ticket_number}</span>
              <span className="font-medium">{ticket.title}</span>
              <StatusBadge status={ticket.status}>{ticketStatusLabel(ticket.status)}</StatusBadge>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
