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
 * The kill switch board, from `GET /api/admin/killswitch`.
 *
 * A client component doing its own read through TanStack Query (§2, decision
 * A), `AuditSearch`'s and `ReviewQueue`'s shape, reading through
 * `lib/wire-read.ts`, which owns the redirect, the status, the the design
 * vocabulary check and the `safeParse` for every board that talks to `app/api/`
 *. Pinned by `tests/killswitch-board.test.tsx::follows the proxy's
 * redirect to /login instead of rendering a board` and the `$name renders the
 * read message, no table and no empty state` rows.
 *
 * The read is fired on paint and takes NO argument of any kind (decision A):
 * which board this caller sees is the token's tenant, decided behind the MCP
 * boundary, so a page naming one would be a project rule written into the browser.
 * It is one Redis `MGET` inside the tool (24ca decision C), which is why it
 * needs no click to start it. Pinned by `::reads the proxy once on paint, as a
 * GET with no query and no body`.
 *
 * The one thing on this surface that writes is `KillSwitchToggle`, an earlier change's,
 * and a project rule holds there without an exception: its mutation is
 * `set_kill_switch`, a typed MCP tool behind RBAC and the protected-tool guard,
 * reached through 24cb's proxy under the caller's own bearer. Nothing here
 * decides anything either — which tools are on the board, in what order,
 * whether each is stopped and who may see any of it all come from 24ca's tool.
 * This renders.
 *
 * The rows are painted in the payload's own order, which is the registry's:
 * never sorted, filtered or grouped here, so a tool added by a later plan
 * cannot land somewhere an operator does not look. Pinned over two payload
 * orders by `::renders a payload in $name order exactly as it arrived`.
 *
 * A failed read THROWS rather than answering an empty board, so the alert and
 * the empty state are mutually exclusive by construction: `No tools are
 * registered.` over a broken proxy is the one sentence on this page an operator
 * acts on by concluding the platform is clean. Pinned from both sides by
 * `::renders the named empty state with no table and no alert` and the three
 * `$name renders the read message, no table and no empty state` rows.
 *
 * Accessible names: the region is `Kill switches`, the board is the table `Tool
 * kill switches` and its columns are `Tool`, `Status`, `Platform-wide`, `This
 * tenant` and `Actions`. Both names are `aria-label`s, because neither role
 * takes its name from its contents.
 */

/**
 * The five columns, in the order every case in the two test files reads them
 * by.
 *
 * `Actions` is APPENDED and never inserted (24db decision A): every cell
 * assertion in `tests/killswitch-board.test.tsx` is positional, so a column
 * landing anywhere but last would make four layer and status cases read the
 * wrong cell while staying green on the header. The controls live in their own
 * column rather than inside a layer cell for the same reason — the layer cells
 * are EVIDENCE, and `cellText(row, GLOBAL_CELL)` reading `No switch Stop
 * platform-wide` is a change those cases are not about.
 */
const COLUMNS = ["Tool", "Status", "Platform-wide", "This tenant", "Actions"] as const;

/** What a layer holding no switch shows, rather than an empty cell. */
const NO_SWITCH = "No switch";

export function KillSwitchBoard() {
  const board = useKillSwitchBoard();

  /**
   * What a successful toggle does, and the only thing it is told about the
   * cache (24db decision G): the toggle takes a callback and imports nothing
   * from here, so the two modules cannot form a cycle.
   */
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
 * One line per registered tool: its name, whether it is stopped, and both
 * layers side by side.
 *
 * The two layers are their own columns rather than one merged state (decision
 * C, 24ca decision B's own reason): an admin clearing the tenant switch has to
 * be told the global one still holds the tool down, and a merged column could
 * not say so.
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
              {/* `protected` reaches the browser as a field of the row so
                  24db's disabled toggle reads it here rather than from a
                  second browser-side copy of `PROTECTED_TOOLS`. */}
              {row.protected ? (
                <span className="ml-2 text-xs text-muted-foreground">Protected</span>
              ) : null}
              {/* The only surface a bad key can be seen from (24ca decision
                  D): the tool nulls both layer states on such a row, so
                  without this marker the line reads as a legitimately
                  stopped tool and nobody goes looking for the key. */}
              {row.unreadable ? (
                <span className="ml-2 text-xs text-muted-foreground">Switch value unreadable</span>
              ) : null}
            </TableCell>
            {/* Read off `stopped` and NEVER recomputed from the two layer
                states (decision C): `stopped` is true when either layer holds
                the tool down OR the stored value will not parse, and only the
                tool knows that precedence. A board deriving it would render
                an unreadable row as running while every call to that tool is
                denied (§5's first risk), which
                `tests/killswitch-board.test.tsx::reads $status for stopped
                true with both states null and unreadable false` pins. The tone
                is hand-picked (plan 39b): a stopped tool reads danger and a
                running one success. */}
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
            {/* Everything this row can DO, in its own cell (24db decision A).
                The toggle holds its own reason, its own arming and its own
                in-flight mutation, so one tool being stopped does not grey
                out another row's controls. */}
            <TableCell>
              <KillSwitchToggle row={row} onChanged={onChanged} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/**
 * One layer's three facts — why, who and when — or the absence of a switch.
 *
 * A null layer says `No switch` rather than drawing nothing: on the one screen
 * whose job is saying WHICH layer holds a tool down, an operator cannot tell an
 * empty cell from a cell that failed to draw.
 */
function LayerCell({ state }: { state: SwitchState | null }) {
  if (state === null) {
    return <span className="text-muted-foreground">{NO_SWITCH}</span>;
  }
  return (
    <div className="flex flex-col gap-0.5">
      <span>{state.reason}</span>
      <span className="text-xs text-muted-foreground">{state.actor}</span>
      {/* Never the raw wire string: decision B is what turns
          `2026-09-12T08:00:00+00:00` into a date and a minute an operator
          reads, and the format itself is pinned by
          `tests/killswitch-format.test.ts`. */}
      <span className="text-xs text-muted-foreground">{formatSwitchInstant(state.at)}</span>
    </div>
  );
}

/**
 * The sentence for whatever went wrong, one alert either way.
 *
 * No override on this surface, unlike `AuditSearch`'s `VALIDATION`: the two
 * codes reachable here are `FORBIDDEN` and `TOOL_DISABLED`, and both shared
 * sentences are true of this read.
 */
function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return KILL_SWITCH_READ_MESSAGE;
}
