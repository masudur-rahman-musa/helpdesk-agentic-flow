import { PRINCIPAL_LABELS, principalLabel } from "@/lib/decision-labels";

/** Role labels in a fixed order, not the order the server sent them. */
export function orderedLabels(principals: string[]): string[] {
  const known = Object.keys(PRINCIPAL_LABELS).filter((key) => principals.includes(key));
  const unknown = principals.filter((key) => !(key in PRINCIPAL_LABELS));
  return [...known, ...unknown].map(principalLabel);
}
