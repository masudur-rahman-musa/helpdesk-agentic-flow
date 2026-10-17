import { type RequestCitation } from "@/features/access/schema";
import type { PendingApproval } from "@/features/approvals/schema";
import { reasonSentence, slotLabel } from "@/lib/decision-labels";

/**
 * A pending request's three nested lists: `Reasons`, `Approvers` and
 * `Sources`. Each list is left out when it holds nothing.
 */
export function ApprovalDetails({ approval }: { approval: PendingApproval }) {
  return (
    <>
      {approval.decision_reasons.length > 0 ? (
        <ul aria-label="Reasons" className="flex flex-col gap-1">
          {approval.decision_reasons.map((reason) => (
            <li key={reason}>{reasonSentence(reason)}</li>
          ))}
        </ul>
      ) : null}

      {approval.approvals.length > 0 ? (
        <ul aria-label="Approvers" className="flex flex-col gap-1">
          {approval.approvals.map((seat, index) => (
            <li key={`${seat.slot}-${index}`} className="flex flex-wrap gap-2">
              <span className="font-medium">{slotLabel(seat.slot)}</span>
              {/* The caller is told WHICH seat they are being asked to fill, off
                    `my_slot` and never off "the seat with a user_id" or "the
                    first one" — both of which mark the wrong seat as soon as a
                    chain has two (`::marks exactly the seat whose slot equals
                    my_slot, and no other seat`). */}
              {seat.slot === approval.my_slot ? (
                <span className="font-medium">Yours to decide</span>
              ) : (
                <span className="text-muted-foreground">
                  {seat.user_id === null ? "not yet assigned" : "assigned"}
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      {approval.citations.length > 0 ? (
        <ul aria-label="Sources" className="flex flex-col gap-1">
          {approval.citations.map((citation) => (
            <CitationRow key={citation.chunk_id} citation={citation} />
          ))}
        </ul>
      ) : null}
    </>
  );
}

/**
 * One stored citation: the title, the version it was decided against, and the
 * heading it came from — the design's "retrieved chunks". The version is
 * load-bearing: two versions of one policy read identically without it, and
 * the approver cannot tell which text the engine decided against.
 */
function CitationRow({ citation }: { citation: RequestCitation }) {
  return (
    <li className="flex flex-wrap gap-2">
      <span className="font-medium">{citation.title}</span>
      <span className="text-xs text-muted-foreground">{citation.version}</span>
      <span className="text-xs text-muted-foreground">{citation.heading_path}</span>
    </li>
  );
}
