"use client";

import { useState } from "react";

import { ApprovalDetails } from "@/features/approvals/components/approval-details";
import {
  DecisionRefusedError,
  useApprovals,
  useApproveRequest,
  useRejectRequest,
} from "@/features/approvals/api";
import type { PendingApproval } from "@/features/approvals/schema";
import { failureMessage } from "@/lib/failure-messages";
import { decisionLabel, riskLabel, statusLabel } from "@/lib/decision-labels";
import { StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Alert } from "@/components/ui/alert";
import { TextArea } from "@/components/ui/field";
import { formatIsoDate } from "@/lib/date-format";

/**
 * The approver's queue and its two actions. A failed read shows an alert, never
 * "nothing waiting". No `maxLength` on the text fields: the server checks
 * length, and only a blank reject reason is caught here.
 */

/** The shared sentence is about chat messages. */
export const DECISION_VALIDATION_MESSAGE =
  "That decision could not be filed. Check the comment and try again.";

const BLANK_REASON_MESSAGE = "Give a reason before rejecting this request.";

export function ApprovalQueue() {
  const approvals = useApprovals();

  return (
    <section aria-label="Approvals" className="flex max-w-3xl flex-col gap-4">
      <PageHeader title="Approvals" />

      {approvals.isError ? (
        <Alert>The approval queue could not be read. Try again in a moment.</Alert>
      ) : null}

      {approvals.isSuccess && approvals.data.length === 0 ? (
        <EmptyState title="Nothing is waiting for your decision." />
      ) : null}

      {approvals.isSuccess && approvals.data.length > 0 ? (
        <ul aria-label="Pending approvals" className="flex flex-col gap-3">
          {approvals.data.map((approval) => (
            <ApprovalRow key={approval.request_id} approval={approval} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function ApprovalRow({ approval }: { approval: PendingApproval }) {
  return (
    <Card as="li" className="flex flex-col gap-2 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground tabular-nums">{approval.request_number}</span>
        <span className="font-medium">{approval.role_name}</span>
        <span className="text-muted-foreground">{approval.system_name}</span>
        <span className="text-muted-foreground">{approval.target_display_name}</span>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
        <span>Asked for by {approval.requester_display_name}</span>
        <span>Asked {formatIsoDate(approval.created_at)}</span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{decisionLabel(approval.decision)}</span>
        <span className="text-muted-foreground">{riskLabel(approval.risk_class)}</span>
        <StatusBadge status={approval.status}>{statusLabel(approval.status)}</StatusBadge>
      </div>

      <p className="text-muted-foreground">{approval.justification}</p>

      {approval.requested_expiry_days !== null ? (
        <p className="text-muted-foreground">{approval.requested_expiry_days} days requested</p>
      ) : null}

      <ApprovalDetails approval={approval} />

      <DecideControls approval={approval} />
    </Card>
  );
}

/**
 * Pending state is per row: one flag for the whole queue would grey out every
 * row. The real guard against a double decision is the server's idempotency
 * key. A success re-reads the queue, since the server decides what the row
 * becomes.
 */
function DecideControls({ approval }: { approval: PendingApproval }) {
  const [comment, setComment] = useState("");
  const [reason, setReason] = useState("");
  const [blankReason, setBlankReason] = useState(false);

  const approve = useApproveRequest(approval.request_id);
  const reject = useRejectRequest(approval.request_id);

  const pending = approve.isPending || reject.isPending;
  const refusal = decisionRefusal(approve.error) ?? decisionRefusal(reject.error);
  const message = blankReason ? BLANK_REASON_MESSAGE : refusal;

  function onApprove(): void {
    setBlankReason(false);
    approve.mutate(comment.trim() === "" ? null : comment);
  }

  function onReject(): void {
    // Caught here to save a round trip. Trimmed, so three spaces do not pass.
    if (reason.trim() === "") {
      setBlankReason(true);
      return;
    }
    setBlankReason(false);
    reject.mutate(reason);
  }

  return (
    <div className="flex flex-col gap-2">
      {message !== null ? <Alert>{message}</Alert> : null}

      <TextArea
        aria-label={`Comment for ${approval.role_name}`}
        rows={2}
        value={comment}
        onChange={(event) => setComment(event.target.value)}
      />
      <TextArea
        aria-label={`Reason for ${approval.role_name}`}
        rows={2}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />

      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          aria-label={`Approve ${approval.role_name}`}
          disabled={pending}
          onClick={onApprove}
        >
          Approve
        </Button>
        <Button
          type="button"
          aria-label={`Reject ${approval.role_name}`}
          disabled={pending}
          onClick={onReject}
        >
          Reject
        </Button>
      </div>
    </div>
  );
}

function decisionRefusal(error: unknown): string | null {
  if (error === null || error === undefined) {
    return null;
  }
  const code = error instanceof DecisionRefusedError ? error.code : undefined;
  return code === "VALIDATION" ? DECISION_VALIDATION_MESSAGE : failureMessage(code);
}
