"use client";

import type { DecisionCard as Card } from "@/features/chat/components/types";
import {
  decisionLabel,
  reasonSentence,
  riskLabel,
  slotLabel,
  statusLabel,
} from "@/lib/decision-labels";
import { formatIsoDate } from "@/lib/date-format";

/**
 * What the policy engine decided, in words a person reads. Display only: the
 * engine set every field. The request id stays in `data-request-id`, since a
 * raw uuid means nothing to the reader. No status line when nothing was filed,
 * so the user is never told a request exists when it does not.
 */
export function DecisionCard({ card }: { card: Card }) {
  return (
    <section
      aria-label="Decision"
      data-request-id={card.requestId ?? undefined}
      className="rounded-md bg-muted p-4"
    >
      <h2 className="text-sm font-semibold">Decision</h2>

      <div className="mt-2 flex flex-wrap gap-2 text-sm">
        <span className="font-medium">{decisionLabel(card.decision)}</span>
        <span className="text-muted-foreground">{riskLabel(card.riskClass)}</span>
        {card.status !== null ? (
          <span className="text-muted-foreground">{statusLabel(card.status)}</span>
        ) : null}
      </div>

      <ul className="mt-2 flex flex-col gap-1 text-sm">
        {card.reasons.map((reason) => (
          <li key={reason}>{reasonSentence(reason)}</li>
        ))}
      </ul>

      <p className="mt-2 text-sm">
        Expires in {card.effectiveExpiryDays} days
        {card.expiresAt !== null ? ` (until ${formatIsoDate(card.expiresAt)})` : ""}
      </p>

      <ul aria-label="Approvers" className="mt-2 flex flex-col gap-1 text-sm">
        {card.approvers.map((approver, index) => (
          <li key={`${approver.slot}-${index}`} className="flex flex-wrap gap-2">
            <span className="font-medium">{slotLabel(approver.slot)}</span>
            <span className="text-muted-foreground">
              {approver.resolved ? "assigned" : "not yet assigned"}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
