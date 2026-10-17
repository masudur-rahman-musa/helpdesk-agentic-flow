import { useQuery } from "@tanstack/react-query";

import { type SodRuleRow, sodRulesBoardSchema } from "@/features/admin/rules/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/**
 * What the board answered, or `null` while the browser navigates away.
 *
 * The null is the redirect and nothing else: rendering the empty state for an
 * expired session would tell somebody who is not even signed in that the tenant
 * has no separation of duties at all, and rendering the alert would blame a
 * read that was fine.
 */
export type BoardAnswer = SodRuleRow[] | null;

/** The SoD rules board, read through TanStack Query. */
export function useRulesBoard() {
  return useQuery({ queryKey: queryKeys.rulesBoard, queryFn: fetchBoard });
}

/**
 * Every rule the tenant has, in the tool's own order, or `null` while the
 * browser follows the proxy's redirect to `/login`.
 */
async function fetchBoard(): Promise<BoardAnswer> {
  const parsed = await readWire(API.rules, sodRulesBoardSchema);
  return parsed === null ? null : parsed.rules;
}
