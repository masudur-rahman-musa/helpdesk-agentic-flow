"use client";

import { useQuery } from "@tanstack/react-query";

import { type MyRequest, type RequestCitation, myRequestsSchema } from "@/lib/access-schema";
import {
  decisionLabel,
  reasonSentence,
  riskLabel,
  slotLabel,
  statusLabel,
} from "@/lib/decision-labels";

/**
 * Everything this user has asked for, newest read of `GET /api/me/requests`
 *.
 *
 * A client component doing its own read through TanStack Query (decision A),
 * exactly as `ChatPanel` reads its history: the bearer stays an httpOnly
 * cookie the proxy attaches, and the expired-session path is
 * `response.redirected` and never a status, because the browser has already
 * followed the proxy's redirect by the time this code runs (22b decision E,
 * pinned by `tests/requests-page.test.tsx`'s `an expired session` case).
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
      <h1 className="text-lg font-semibold">My requests</h1>

      {requests.isError ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          Your requests could not be read. Try again in a moment.
        </p>
      ) : null}

      {requests.isSuccess && requests.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">You have not asked for any access yet.</p>
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
    <li className="flex flex-col gap-2 rounded-md border border-border p-4 text-sm">
      <div className="flex flex-wrap gap-2">
        <span className="font-medium">{request.role_name}</span>
        <span className="text-muted-foreground">{request.system_name}</span>
        <span className="text-muted-foreground">{request.target_display_name}</span>
      </div>

      <div className="flex flex-wrap gap-2">
        <span className="font-medium">{decisionLabel(request.decision)}</span>
        <span className="text-muted-foreground">{riskLabel(request.risk_class)}</span>
        <span className="text-muted-foreground">{statusLabel(request.status)}</span>
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
    </li>
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

async function fetchRequests(): Promise<MyRequest[]> {
  const response = await fetch("/api/me/requests", { headers: { accept: "application/json" } });

  if (response.redirected) {
    window.location.assign(response.url);
    return [];
  }
  if (!response.ok) {
    // Thrown rather than swallowed: a proxy answering `502
    // UPSTREAM_UNAVAILABLE` carries a perfectly valid JSON body, so a reader
    // that skipped the status would parse a refusal envelope into no rows.
    throw new Error(`your requests could not be read (${response.status})`);
  }

  const parsed = myRequestsSchema.safeParse(await response.json());
  if (!parsed.success) {
    throw new Error("the requests payload did not match what this page reads");
  }
  return parsed.data.requests;
}
