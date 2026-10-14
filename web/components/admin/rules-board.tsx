"use client";

import { useQuery } from "@tanstack/react-query";

import { failureMessage } from "@/lib/chat-errors";
import { RULES_READ_MESSAGE, UNRESOLVED_ROLE_TEXT } from "@/lib/rules-labels";
import { type SodRuleRoleBlock, type SodRuleRow, sodRulesBoardSchema } from "@/lib/rules-schema";
import { readWire, WireRefusedError } from "@/lib/wire-read";
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

/**
 * The separation of duties board, from `GET /api/admin/rules`.
 *
 * A client component doing its own read through TanStack Query (§2, decision
 * A), `KillSwitchBoard`'s and `AuditSearch`'s shape — with the read itself
 * moved into `lib/wire-read.ts`, which this page is the first caller of
 * (decision C). The bearer stays an httpOnly cookie the proxy attaches, and an
 * expired session arrives as `response.redirected` rather than as a status,
 * because the browser has already followed the proxy's redirect by the time
 * this code runs (22b decision E). Pinned by
 * `tests/rules-board.test.tsx::follows the proxy's redirect to /login instead
 * of rendering a board`.
 *
 * The read is fired on paint and takes NO argument of any kind (decision A):
 * which rules this caller sees is the token's tenant, decided behind the MCP
 * boundary, so a page naming one would be a project rule written into the browser.
 * Pinned by `::reads the proxy once on paint, as a GET with no query and no
 * body`.
 *
 * Everything here is a READ — v1 is read-only (the design), so this surface holds
 * no control at all and a project rule has nothing to bite on. Nothing is decided
 * here either: which rules the board contains, their order, and whether each
 * side resolves all come from 24ea's tool. This renders.
 *
 * The rows are painted in the payload's own order, which is the tool's total
 * order (`policy_ref` ascending then `rule_id` ascending, 24ea decision C):
 * never sorted, filtered or grouped here, so a rule added by a later plan
 * cannot land somewhere an admin does not look (§5's first risk). Pinned over
 * two payload orders by `::renders a payload in $name order exactly as it
 * arrived`.
 *
 * A failed read THROWS inside `readWire`, so the alert and the empty state are
 * mutually exclusive by construction: `No separation of duties rules are
 * configured.` over a broken proxy is the one sentence on this page an admin
 * acts on by concluding the tenant has no SoD control at all. Pinned from both
 * sides by `::renders the named empty state with no table and no alert` and the
 * three `$name renders the read message, no table and no empty state` rows.
 *
 * Accessible names: the region is `Rules` and the board is the table
 * `Separation of duties rules`. Both are `aria-label`s, because neither role
 * takes its name from its contents.
 */

/**
 * The four columns, in the order every cell assertion in
 * `tests/rules-board.test.tsx` reads them by.
 *
 * No `Rule` column of ids (decision B): v1 takes no rule id anywhere, and a
 * 36-character uuid in the first column costs the rationale its width on every
 * row. A rule is identified here by its two roles and its policy ref — the same
 * words an SoD refusal quotes to the requester.
 */
const COLUMNS = ["Role A", "Role B", "Rationale", "Policy"] as const;

/** The query this board reads. */
const BOARD_KEY = ["admin", "rules", "board"] as const;

/**
 * What the board answered, or `null` while the browser navigates away.
 *
 * The null is the redirect and nothing else: rendering the empty state for an
 * expired session would tell somebody who is not even signed in that the tenant
 * has no separation of duties at all, and rendering the alert would blame a
 * read that was fine.
 */
type BoardAnswer = SodRuleRow[] | null;

export function RulesBoard() {
  const board = useQuery({ queryKey: BOARD_KEY, queryFn: fetchBoard });

  const refusal = board.isError ? errorMessage(board.error) : null;
  const rows = board.isSuccess ? board.data : null;

  return (
    <section aria-label="Rules" className="flex max-w-5xl flex-col gap-4">
      <PageHeader title="Separation of duties rules" />

      {refusal !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {refusal}
        </p>
      ) : null}

      {rows !== null && rows.length === 0 ? (
        <EmptyState title="No separation of duties rules are configured." />
      ) : null}

      {rows !== null && rows.length > 0 ? <RulesTable rows={rows} /> : null}
    </section>
  );
}

/**
 * One line per rule: both sides side by side, then why the pair is kept apart
 * and which policy says so.
 *
 * The two sides are their own columns rather than one merged cell, because the
 * pair is the rule: an admin reading this page is asking which two seats one
 * person may not hold, and a merged cell could not say which of the two failed
 * to resolve.
 */
function RulesTable({ rows }: { rows: SodRuleRow[] }) {
  return (
    <Table aria-label="Separation of duties rules">
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
          <TableRow key={row.rule_id} className="align-top">
            <TableCell>
              <RoleCell block={row.role_a} roleId={row.role_a_id} />
            </TableCell>
            <TableCell>
              <RoleCell block={row.role_b} roleId={row.role_b_id} />
            </TableCell>
            <TableCell>{row.rationale}</TableCell>
            <TableCell>{row.policy_ref}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/**
 * One side of a rule: three of the block's five fields, or the id and the
 * marker when the tool could not resolve it.
 *
 * The role name alone is not enough — two systems may name a role the same
 * thing — so the slug and the system name ride with it. Neither `role_id` nor
 * `system_slug` is rendered (decision B): the id has no control to feed on this
 * page, and the system slug says nothing the system name has not already said
 * in words. Pinned as ABSENCES by `tests/rules-board.test.tsx::renders a
 * resolved side's name, slug and system name, and neither id nor system slug`.
 *
 * An unresolved side shows its own id BESIDE the marker (decision F): the id is
 * what a support request or a `psql` query needs, and each side is decided on
 * its own, so a row whose two sides are both unresolved carries both markers
 * rather than one.
 */
function RoleCell({ block, roleId }: { block: SodRuleRoleBlock | null; roleId: string }) {
  if (block === null) {
    return (
      <div className="flex flex-col gap-0.5">
        <span className="font-mono text-xs">{roleId}</span>
        <span className="text-xs text-muted-foreground">{UNRESOLVED_ROLE_TEXT}</span>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-0.5">
      <span>{block.name}</span>
      <span className="text-xs text-muted-foreground">{block.slug}</span>
      <span className="text-xs text-muted-foreground">{block.system_name}</span>
    </div>
  );
}

/**
 * The sentence for whatever went wrong, one alert either way.
 *
 * A `WireRefusedError` carries one of the design's eight codes and gets that
 * code's shared sentence; everything else — a payload the parser refused, a
 * body that is not JSON, or a code outside that vocabulary such as the proxy's
 * own `UPSTREAM_UNAVAILABLE` — is a failed READ and gets `RULES_READ_MESSAGE`
 * (a project rule, §1's discriminator). No override on this surface: the two
 * codes reachable here are `FORBIDDEN` and `TOOL_DISABLED`, and both shared
 * sentences are true of this read.
 */
function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return RULES_READ_MESSAGE;
}

/**
 * Every rule the tenant has, in the tool's own order, or `null` while the
 * browser follows the proxy's redirect to `/login`.
 */
async function fetchBoard(): Promise<BoardAnswer> {
  const parsed = await readWire("/api/admin/rules", sodRulesBoardSchema);
  return parsed === null ? null : parsed.rules;
}
