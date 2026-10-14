"use client";

import { useQuery } from "@tanstack/react-query";

import { type MyRequest, type RequestCitation, myRequestsSchema } from "@/features/access/schema";
import {
  decisionLabel,
  reasonSentence,
  riskLabel,
  slotLabel,
  statusLabel,
} from "@/features/chat/decision-labels";
import { readWire } from "@/lib/wire-read";
import { StatusBadge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

/**
 * Everything this user has asked for, newest read of `GET /api/me/requests`
 *.
 *
 * A client component doing its own read through TanStack Query (decision A),
 * with the read itself in `lib/wire-read.ts`: the redirect, the
 * status, the typed code and the parse live there once, and this file maps that
 * module's `null` — the browser already following the proxy's redirect to
 * `/login` (22b decision E) — to the empty list. Pinned by
 * `tests/requests-page.test.tsx`'s `an expired session` case.
 *
 * A failed read THROWS rather than answering an empty list. The two are
 * different things to the person reading the page — "you have filed nothing"
 * over a broken proxy is the one sentence here somebody might act on — so the
 * empty state and the alert are mutually exclusive by construction, asserted
 * from both sides by `::renders the named empty state and no list` and the two
 * `a read that fails` rows.
 *
 * Accessible names: the region is `My requests`, the rows are the list
 * `Requests`, and inside a row the three nested lists are `Reasons`,
 * `Approvers` and `Sources` — a row holding three lists is otherwise not
 * countable, which is why each carries its own name.
 *
 * Every enum value is rendered through `decision-labels.ts`: `approval`,
 * `elevated` and `pending` are the engine's vocabulary, not a person's. Dates
 * are the ISO prefix, never `toLocaleDateString`, which answers "12/1/2026" —
 * a different day in half the world.
 */
export function RequestList() {
  const requests = useQuery({ queryKey: ["me", "requests"], queryFn: fetchRequests });

  return (
    <section aria-label="My requests" className="flex max-w-3xl flex-col gap-4">
      <PageHeader title="My requests" />

      {requests.isError ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          Your requests could not be read. Try again in a moment.
        </p>
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
        <p className="text-muted-foreground">Expires {expiry.slice(0, 10)}</p>
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
 * One stored citation: the title, the version it was decided against, and the
 * heading it came from. The version is load-bearing — two versions of one
 * policy read identically without it (`::renders each citation as its own
 * source, naming the document and its version`).
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

/**
 * Every request this user has filed, or the empty list while the browser
 * follows the proxy's redirect to `/login`.
 *
 * A refusal and a failed read both THROW out of `readWire`, and this page
 * dresses neither (24ia decision B): the section's own alert sentence is what a
 * requester reads for a typed 403 and for a bodyless 500 alike, pinned by
 * `tests/requests-page.test.tsx::a 403 FORBIDDEN renders this page's own read
 * sentence, not the code's`.
 */
async function fetchRequests(): Promise<MyRequest[]> {
  const parsed = await readWire("/api/me/requests", myRequestsSchema);
  return parsed === null ? [] : parsed.requests;
}
