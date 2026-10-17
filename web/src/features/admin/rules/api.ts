import { useQuery } from "@tanstack/react-query";

import { type SodRuleRow, sodRulesBoardSchema } from "@/features/admin/rules/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** `null` only while the browser follows the redirect to `/login`. */
export type BoardAnswer = SodRuleRow[] | null;

export function useRulesBoard() {
  return useQuery({ queryKey: queryKeys.rulesBoard, queryFn: fetchBoard });
}

async function fetchBoard(): Promise<BoardAnswer> {
  const parsed = await readWire(API.rules, sodRulesBoardSchema);
  return parsed === null ? null : parsed.rules;
}
