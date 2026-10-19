import type { TicketStatus } from "@/features/tickets/schema";

const STATUS_LABELS: Record<TicketStatus, string> = {
  open: "Open",
  solved: "Solved",
};

const KIND_LABELS: ReadonlyMap<string, string> = new Map([
  ["password_reset", "Password reset"],
  ["unlock", "Account unlock"],
  ["general", "General"],
  ["escalation", "Escalation"],
]);

export function ticketStatusLabel(status: TicketStatus): string {
  return STATUS_LABELS[status];
}

/** A kind the page does not know shows as "Other", never as the wire word. */
export function ticketKindLabel(kind: string): string {
  return KIND_LABELS.get(kind) ?? "Other";
}
