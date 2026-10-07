"use client";

import type { DecisionCard as Card } from "@/components/chat/types";
import {
  decisionLabel,
  reasonSentence,
  riskLabel,
  slotLabel,
  statusLabel,
} from "@/lib/decision-labels";

/**
 * What the engine decided about this turn's access, in the words a person
 * reads (the design, an earlier change).
 *
 * Presentational: a card in, markup out. No fetch, no state, no clock — the
 * engine decided every field before the card crossed the wire, and this
 * component's only job is to choose the label. Its accessible name is
 * `Decision` and the approver chain inside it is a list named `Approvers`;
 * both are how `tests/chat-page.test.tsx`'s `ChatPanel — the decision card`
 * block finds them, and the second is what makes "exactly one `listitem` per
 * approver" countable beside the reasons.
 *
 * Five of the design's six fields are here. The sixth, the citations, stays in
 * an earlier change's `Sources` section directly beneath (decision A).
 *
 * Two things are deliberately NOT rendered as text. The request id rides as
 * `data-request-id` on the region and nowhere else — row 23's `/requests` is
 * what needs the handle, and a raw uuid reads as nothing to the person asking
 * — and the SoD rule id is dropped by `reasonSentence` before it reaches here
 * (decision C). Both absences are asserted by `::renders the decision, risk,
 * reasons, expiry, approvers and status of a filed request`.
 *
 * The status line is the one conditional row: a turn that classified and filed
 * nothing has no status to name, and defaulting it would tell a user something
 * was filed for them when nothing was (`::a classified request shows no status
 * line`). The date is the ISO prefix of `expires_at`, never a locale format:
 * `toLocaleDateString` answers "12/1/2026", which is a different date in half
 * the world.
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
        {card.expiresAt !== null ? ` (until ${card.expiresAt.slice(0, 10)})` : ""}
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
