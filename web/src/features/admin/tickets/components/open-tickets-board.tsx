"use client";

import { failureMessage } from "@/lib/failure-messages";
import { formatIsoDate } from "@/lib/date-format";
import { SolveRefusedError, useOpenTickets, useSolveTicket } from "@/features/admin/tickets/api";
import type { OpenTicket } from "@/features/admin/tickets/schema";
import { ticketKindLabel } from "@/features/tickets/labels";
import { WireRefusedError } from "@/lib/wire-read";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

/**
 * The open tickets board, with a Solve button on each row. Rows keep the
 * server's order, oldest first. A failed read shows an alert, never "no tickets are open", which an
 * admin would read as nothing left to do.
 */

const READ_MESSAGE = "Open tickets could not be read. Try again in a moment.";

/** Not the shared default: a solve that failed on the way may still have landed. */
const SOLVE_FAILED_MESSAGE = "The ticket could not be solved. Try again in a moment.";

/** No id column: a ticket is known by its number. */
const COLUMNS = ["Ticket", "Title", "Kind", "Opened by", "Opened", "Solve"] as const;

export function OpenTicketsBoard() {
  const board = useOpenTickets();

  const refusal = board.isError ? errorMessage(board.error) : null;
  const rows = board.isSuccess ? board.data : null;

  return (
    <section aria-label="Open tickets" className="flex max-w-5xl flex-col gap-4">
      <PageHeader title="Open tickets" />

      {refusal !== null ? <Alert>{refusal}</Alert> : null}

      {rows !== null && rows.length === 0 ? <EmptyState title="No tickets are open." /> : null}

      {rows !== null && rows.length > 0 ? <TicketsTable rows={rows} /> : null}
    </section>
  );
}

function TicketsTable({ rows }: { rows: OpenTicket[] }) {
  return (
    <Table aria-label="Open tickets">
      <TableHeader>
        <TableRow>
          {COLUMNS.map((column) => (
            <TableHead key={column} scope="col">
              {column}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.ticket_id} className="align-top">
            <TableCell className="font-mono text-xs">{row.ticket_number}</TableCell>
            <TableCell>{row.title}</TableCell>
            <TableCell>{ticketKindLabel(row.kind)}</TableCell>
            <TableCell>{row.opened_by}</TableCell>
            <TableCell>{formatIsoDate(row.created_at)}</TableCell>
            <TableCell>
              <SolveButton row={row} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/** Each row has its own solve, so one running solve leaves the others live. */
function SolveButton({ row }: { row: OpenTicket }) {
  const solve = useSolveTicket(row.ticket_id);
  const refusal = solve.isError ? solveRefusal(solve.error) : null;

  return (
    <div className="flex flex-col gap-1">
      <Button
        type="button"
        size="sm"
        aria-label={`Solve ${row.ticket_number}`}
        disabled={solve.isPending}
        onClick={() => solve.mutate()}
      >
        Solve
      </Button>

      {refusal !== null ? (
        <p role="alert" className="text-xs">
          {refusal}
        </p>
      ) : null}
    </div>
  );
}

function solveRefusal(error: unknown): string {
  if (error instanceof SolveRefusedError) {
    return failureMessage(error.code);
  }
  return SOLVE_FAILED_MESSAGE;
}

function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return READ_MESSAGE;
}
