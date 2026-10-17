"use client";

import type { MyRequest, RequestCitation } from "@/features/access/schema";
import {
  decisionLabel,
  reasonSentence,
  riskLabel,
  slotLabel,
  statusLabel,
} from "@/lib/decision-labels";
import { useMyRequests } from "@/features/requests/api";
import { StatusBadge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Alert } from "@/components/ui/alert";
import { formatIsoDate } from "@/lib/date-format";

/**
 * Everything this user has asked for. A redirect to `/login` shows as an empty
 * list, but a failed read shows an alert, never "you have filed nothing", since
 * that is a sentence someone might act on. Enum values go through the label
 * helpers, because `elevated` and `pending` are engine words.
 */
export function RequestList() {
  const requests = useMyRequests();

  return (
    <section aria-label="My requests" className="flex max-w-3xl flex-col gap-4">
      <PageHeader title="My requests" />

      {requests.isError ? (
        <Alert>Your requests could not be read. Try again in a moment.</Alert>
      ) : null}

      {requests.isSuccess && requests.data.length === 0 ? (
        <EmptyState title="You have not asked for any access yet." />
      ) : null}

      {requests.isSuccess && requests.data.length > 0 ? (
        <ul aria-label="Requests" className="flex flex-col gap-3">
          {requests.data.map((request) => (
            <RequestRow key={request.request_id} request={request} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function RequestRow({ request }: { request: MyRequest }) {
  const expiry = request.grant_id !== null ? request.expires_at : null;

  return (
    <Card as="li" className="flex flex-col gap-2 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{request.role_name}</span>
        <span className="text-muted-foreground">{request.system_name}</span>
        <span className="text-muted-foreground">{request.target_display_name}</span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{decisionLabel(request.decision)}</span>
        <span className="text-muted-foreground">{riskLabel(request.risk_class)}</span>
        <StatusBadge status={request.status}>{statusLabel(request.status)}</StatusBadge>
      </div>

      {request.requested_expiry_days !== null ? (
        <p className="text-muted-foreground">{request.requested_expiry_days} days requested</p>
      ) : null}

      {/* Only a request that HAS a grant has an expiry to show: a line on a
          pending ask would tell the user access runs until a date nobody
          granted (`::shows the grant's expiry only on the request that has a
          grant`). */}
      {expiry !== null ? (
        <p className="text-muted-foreground">Expires {formatIsoDate(expiry)}</p>
      ) : null}

      {request.decision_reasons.length > 0 ? (
        <ul aria-label="Reasons" className="flex flex-col gap-1">
          {request.decision_reasons.map((reason) => (
            <li key={reason}>{reasonSentence(reason)}</li>
          ))}
        </ul>
      ) : null}

      {request.approvals.length > 0 ? (
        <ul aria-label="Approvers" className="flex flex-col gap-1">
          {request.approvals.map((seat, index) => (
            <li key={`${seat.slot}-${index}`} className="flex flex-wrap gap-2">
              <span className="font-medium">{slotLabel(seat.slot)}</span>
              <span className="text-muted-foreground">
                {seat.user_id === null ? "not yet assigned" : "assigned"}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {request.citations.length > 0 ? (
        <ul aria-label="Sources" className="flex flex-col gap-1">
          {request.citations.map((citation) => (
            <CitationRow key={citation.chunk_id} citation={citation} />
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

/**
 * One stored citation. The version matters: two versions of one policy look the
 * same without it.
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
