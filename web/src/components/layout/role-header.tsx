import { PRINCIPAL_LABELS, principalLabel } from "@/lib/decision-labels";

/** Principals in the enum's order, not the wire's (decision D). */
export function orderedLabels(principals: string[]): string[] {
  const known = Object.keys(PRINCIPAL_LABELS).filter((key) => principals.includes(key));
  const unknown = principals.filter((key) => !(key in PRINCIPAL_LABELS));
  return [...known, ...unknown].map(principalLabel);
}
