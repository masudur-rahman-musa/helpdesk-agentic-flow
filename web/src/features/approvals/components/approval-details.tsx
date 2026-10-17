import { type RequestCitation } from "@/features/access/schema";
import type { PendingApproval } from "@/features/approvals/schema";
import { reasonSentence, slotLabel } from "@/lib/decision-labels";

/** Reasons, approvers and sources. An empty list is left out. */
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
              {/* Marked by `my_slot`, the only safe test once a chain has two seats. */}
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

/** The version matters: two versions of one policy look the same without it. */
function CitationRow({ citation }: { citation: RequestCitation }) {
  return (
    <li className="flex flex-wrap gap-2">
      <span className="font-medium">{citation.title}</span>
      <span className="text-xs text-muted-foreground">{citation.version}</span>
      <span className="text-xs text-muted-foreground">{citation.heading_path}</span>
    </li>
  );
}
