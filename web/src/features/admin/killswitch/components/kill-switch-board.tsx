"use client";

import { failureMessage } from "@/lib/failure-messages";
import { KILL_SWITCH_READ_MESSAGE } from "@/features/admin/killswitch/format";
import { formatSwitchInstant } from "@/lib/date-format";
import { useKillSwitchBoard, useRefreshKillSwitchBoard } from "@/features/admin/killswitch/api";
import type { KillSwitchRow, SwitchState } from "@/features/admin/killswitch/schema";
import { WireRefusedError } from "@/lib/wire-read";
import { KillSwitchToggle } from "@/features/admin/killswitch/components/kill-switch-toggle";
import { Badge } from "@/components/ui/badge";
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

/**
 * The kill switch board. Rows keep the server's order, so a new tool never
 * lands somewhere an operator does not look. A failed read shows an alert,
 * never "no tools", which an operator would read as "all clear".
 */

/** Controls get their own column so the layer cells show only state. */
const COLUMNS = ["Tool", "Status", "Platform-wide", "This tenant", "Actions"] as const;

const NO_SWITCH = "No switch";

export function KillSwitchBoard() {
  const board = useKillSwitchBoard();

  const onChanged = useRefreshKillSwitchBoard();

  const refusal = board.isError ? errorMessage(board.error) : null;
  const rows = board.isSuccess ? board.data : null;

  return (
    <section aria-label="Kill switches" className="flex max-w-5xl flex-col gap-4">
      <PageHeader title="Kill switches" />

      {refusal !== null ? <Alert>{refusal}</Alert> : null}

      {rows !== null && rows.length === 0 ? <EmptyState title="No tools are registered." /> : null}

      {rows !== null && rows.length > 0 ? <SwitchTable rows={rows} onChanged={onChanged} /> : null}
    </section>
  );
}

/**
 * Both layers get their own column: clearing the tenant switch must still
 * show that the platform switch holds the tool down.
 */
function SwitchTable({ rows, onChanged }: { rows: KillSwitchRow[]; onChanged: () => void }) {
  return (
    <Table aria-label="Tool kill switches">
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
          <TableRow key={row.tool} className="align-top">
            <TableCell>
              {row.tool}
              {/* The toggle reads `protected` from the row, never a browser copy. */}
              {row.protected ? (
                <span className="ml-2 text-xs text-muted-foreground">Protected</span>
              ) : null}
              {/* Without this, a bad stored value reads as a normal stop. */}
              {row.unreadable ? (
                <span className="ml-2 text-xs text-muted-foreground">Switch value unreadable</span>
              ) : null}
            </TableCell>
            {/* Only the server knows the precedence, so `stopped` is never recomputed. */}
            <TableCell>
              <Badge tone={row.stopped ? "danger" : "success"}>
                {row.stopped ? "Stopped" : "Running"}
              </Badge>
            </TableCell>
            <TableCell>
              <LayerCell state={row.global_state} />
            </TableCell>
            <TableCell>
              <LayerCell state={row.tenant_state} />
            </TableCell>
            {/* Each row's controls keep their own pending state. */}
            <TableCell>
              <KillSwitchToggle row={row} onChanged={onChanged} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/** An empty layer says `No switch`, so it never looks like a failed draw. */
function LayerCell({ state }: { state: SwitchState | null }) {
  if (state === null) {
    return <span className="text-muted-foreground">{NO_SWITCH}</span>;
  }
  return (
    <div className="flex flex-col gap-0.5">
      <span>{state.reason}</span>
      <span className="text-xs text-muted-foreground">{state.actor}</span>
      {/* Shown in UTC, never the raw server string. */}
      <span className="text-xs text-muted-foreground">{formatSwitchInstant(state.at)}</span>
    </div>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return KILL_SWITCH_READ_MESSAGE;
}
