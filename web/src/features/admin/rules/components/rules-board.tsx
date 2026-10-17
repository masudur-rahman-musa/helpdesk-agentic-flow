"use client";

import { failureMessage } from "@/lib/failure-messages";
import { RULES_READ_MESSAGE, UNRESOLVED_ROLE_TEXT } from "@/features/admin/rules/labels";
import { useRulesBoard } from "@/features/admin/rules/api";
import type { SodRuleRoleBlock, SodRuleRow } from "@/features/admin/rules/schema";
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

/**
 * The separation of duties board, read only. Rows keep the server's order. A
 * failed read shows an alert, never "no rules", which an admin would read as
 * the tenant having no such control.
 */

/** No id column: a rule is known by its two roles and its policy. */
const COLUMNS = ["Role A", "Role B", "Rationale", "Policy"] as const;

export function RulesBoard() {
  const board = useRulesBoard();

  const refusal = board.isError ? errorMessage(board.error) : null;
  const rows = board.isSuccess ? board.data : null;

  return (
    <section aria-label="Rules" className="flex max-w-5xl flex-col gap-4">
      <PageHeader title="Separation of duties rules" />

      {refusal !== null ? <Alert>{refusal}</Alert> : null}

      {rows !== null && rows.length === 0 ? (
        <EmptyState title="No separation of duties rules are configured." />
      ) : null}

      {rows !== null && rows.length > 0 ? <RulesTable rows={rows} /> : null}
    </section>
  );
}

/** Each side has its own column, so it is clear which one failed to resolve. */
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
 * The system name rides with the role, since two systems can share a role name.
 * An unresolved side shows its id beside the marker, so someone can look it up.
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

function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return RULES_READ_MESSAGE;
}
