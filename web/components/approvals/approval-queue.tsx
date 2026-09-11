"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { type RequestCitation } from "@/lib/access-schema";
import { type PendingApproval, pendingApprovalsSchema } from "@/lib/approvals-schema";
import { failureMessage } from "@/lib/chat-errors";
import {
  decisionLabel,
  reasonSentence,
  riskLabel,
  slotLabel,
  statusLabel,
} from "@/lib/decision-labels";
import { Button } from "@/components/ui/field";

/**
 * The queue waiting on this approver, from `GET /api/me/approvals`, and the two
 * decisions it can take.
 *
 * A client component doing its own read and its own mutations through TanStack
 * Query (decision A), exactly `RequestList`'s shape: the bearer stays an
 * httpOnly cookie the proxy attaches, and an expired session is
 * `response.redirected` rather than a status, because the browser has already
 * followed the proxy's redirect by the time this code runs (22b decision E).
 * Both the read AND both mutations check it — a session expires while the
 * queue sits open on a screen far more often than it expires mid-read — which
 * is what `tests/approvals-page.test.tsx`'s `::follows the redirect at the
 * decision too, not only at the read` pins.
 *
 * Every act crosses 23c's proxies, so a project rule holds: the decision is a
 * typed MCP tool behind RBAC, the kill switch, the seat rules and the audit
 * event, and this component is the surface. Nothing here decides anything —
 * the queue's order, its membership, which seat is the caller's and every name
 * on a row come from the tool.
 *
 * A failed read THROWS rather than answering an empty list, so the alert and
 * the empty state are mutually exclusive by construction: `Nothing is waiting
 * for your decision.` over a broken proxy is the sentence on this page an
 * approver acts on by closing the tab. Pinned from both sides by
 * `::renders the named empty state with no list and no alert` and the two
 * `a read that fails` rows.
 *
 * Accessible names: the region is `Approvals`, the rows are the list `Pending
 * approvals`, and inside a row the three nested lists are `Reasons`,
 * `Approvers` and `Sources`. Each row's two controls carry the role name
 * (`Approve {role_name}`, `Comment for {role_name}`) — the `grant-list.tsx`
 * shape — because two rows otherwise carry four controls with the same four
 * names.
 *
 * Neither field declares a `maxLength` (§1, 23c decision E): the tool's
 * `COMMENT_MAX_LENGTH` and `REASON_MAX_LENGTH` are the only validator, so a
 * comment of exactly 2000 characters reaches the proxy whole
 * (`::sends a 2000 character comment whole, with no maxLength on the field`).
 * The one refusal the page answers for itself is a blank rejection reason,
 * which costs a round trip and a rate-limit slot to be told (`::$name makes no
 * call at all`) — a floor, never a second cap.
 */

/**
 * The `VALIDATION` sentence on this surface, and only on this surface
 * (decision B).
 *
 * `FAILURE_MESSAGES.VALIDATION` is "That message could not be sent.", which is
 * about a chat message and says nothing true about a comment the tool refused;
 * 22b's `tests/chat-schema.test.ts` pins every string in that map verbatim, so
 * widening the shared map is not available. Exactly ONE code is overridden —
 * every other sentence still comes from the shared map, so no second opinion
 * about `FORBIDDEN` or `TOOL_DISABLED` can appear here. Pinned by
 * `tests/approvals-page.test.tsx::pins the VALIDATION override sentence
 * exactly`.
 */
export const DECISION_VALIDATION_MESSAGE =
  "That decision could not be filed. Check the comment and try again.";

/** The sentence the page says when a Reject is attempted with no reason. */
const BLANK_REASON_MESSAGE = "Give a reason before rejecting this request.";

const QUEUE_KEY = ["me", "approvals"] as const;

export function ApprovalQueue() {
  const approvals = useQuery({ queryKey: QUEUE_KEY, queryFn: fetchApprovals });

  return (
    <section aria-label="Approvals" className="flex max-w-3xl flex-col gap-4">
      <h1 className="text-lg font-semibold">Approvals</h1>

      {approvals.isError ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          The approval queue could not be read. Try again in a moment.
        </p>
      ) : null}

      {approvals.isSuccess && approvals.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing is waiting for your decision.</p>
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
    <li className="flex flex-col gap-2 rounded-md border border-border p-4 text-sm">
      <div className="flex flex-wrap gap-2">
        <span className="font-medium">{approval.role_name}</span>
        <span className="text-muted-foreground">{approval.system_name}</span>
        <span className="text-muted-foreground">{approval.target_display_name}</span>
      </div>

      <div className="flex flex-wrap gap-2 text-muted-foreground">
        <span>Asked for by {approval.requester_display_name}</span>
        <span>Asked {approval.created_at.slice(0, 10)}</span>
      </div>

      <div className="flex flex-wrap gap-2">
        <span className="font-medium">{decisionLabel(approval.decision)}</span>
        <span className="text-muted-foreground">{riskLabel(approval.risk_class)}</span>
        <span className="text-muted-foreground">{statusLabel(approval.status)}</span>
      </div>

      <p className="text-muted-foreground">{approval.justification}</p>

      {approval.requested_expiry_days !== null ? (
        <p className="text-muted-foreground">{approval.requested_expiry_days} days requested</p>
      ) : null}

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

      <DecideControls approval={approval} />
    </li>
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

/**
 * The two acts, and the only local state this page has: the text in the two
 * fields and which of this ROW's mutations is in flight.
 *
 * Per row on purpose. One in-flight flag for the whole queue would grey out
 * every control while a single row is being decided, which an approver holding
 * ten items pays for ten times; no flag at all leaves the Reject beside an
 * in-flight Approve live, so the opposite decision can be filed on the same
 * request. The guarantee against a double-filed decision is not this: it is the
 * server-derived idempotency key (23c decision B), which replays the stored
 * response rather than filing twice. These disabled controls are a courtesy on
 * top of it, and `::disables both controls of the deciding row while its
 * decision is in flight, and no other row's` says so.
 *
 * A success invalidates the queue and nothing is removed optimistically
 * (decision E): the tool decides what the row becomes — an approval that
 * completed a chain and then failed the fresh the design check answers `failed`
 * with a `failure_code`, and a chain with seats left stays `pending` — so a row
 * vanishing on click would show a decision that did not land as done.
 */
function DecideControls({ approval }: { approval: PendingApproval }) {
  const queryClient = useQueryClient();
  const [comment, setComment] = useState("");
  const [reason, setReason] = useState("");
  const [blankReason, setBlankReason] = useState(false);

  const invalidate = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: QUEUE_KEY });
  };

  const approve = useMutation({
    mutationFn: () =>
      postDecision(approval.request_id, "approve", {
        comment: comment.trim() === "" ? null : comment,
      }),
    onSuccess: invalidate,
  });

  const reject = useMutation({
    mutationFn: () => postDecision(approval.request_id, "reject", { reason }),
    onSuccess: invalidate,
  });

  const pending = approve.isPending || reject.isPending;
  const refusal = decisionRefusal(approve.error) ?? decisionRefusal(reject.error);
  const message = blankReason ? BLANK_REASON_MESSAGE : refusal;

  function onApprove(): void {
    setBlankReason(false);
    approve.mutate();
  }

  function onReject(): void {
    // The floor, and the only refusal answered without a round trip: the tool
    // would spend a rate-limit slot to say `VALIDATION` for something the page
    // already knows. Trimmed, because `reason === ""` lets three spaces past.
    if (reason.trim() === "") {
      setBlankReason(true);
      return;
    }
    setBlankReason(false);
    reject.mutate();
  }

  return (
    <div className="flex flex-col gap-2">
      {message !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {message}
        </p>
      ) : null}

      <textarea
        aria-label={`Comment for ${approval.role_name}`}
        rows={2}
        value={comment}
        onChange={(event) => setComment(event.target.value)}
        className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      <textarea
        aria-label={`Reason for ${approval.role_name}`}
        rows={2}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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

/**
 * A decision the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
class DecisionRefusedError extends Error {
  readonly code: unknown;

  constructor(code: unknown) {
    super("the decision was refused");
    this.code = code;
  }
}

/** The sentence for a refused decision, or `null` while nothing is refused. */
function decisionRefusal(error: unknown): string | null {
  if (error === null || error === undefined) {
    return null;
  }
  const code = error instanceof DecisionRefusedError ? error.code : undefined;
  return code === "VALIDATION" ? DECISION_VALIDATION_MESSAGE : failureMessage(code);
}

async function fetchApprovals(): Promise<PendingApproval[]> {
  const response = await fetch("/api/me/approvals", { headers: { accept: "application/json" } });

  if (response.redirected) {
    window.location.assign(response.url);
    return [];
  }
  if (!response.ok) {
    // Thrown rather than swallowed: the proxy's own `502 UPSTREAM_UNAVAILABLE`
    // is valid JSON, so a reader that skipped the status would turn an outage
    // into "nothing is waiting for your decision".
    throw new Error(`the approval queue could not be read (${response.status})`);
  }

  const parsed = pendingApprovalsSchema.safeParse(await response.json());
  if (!parsed.success) {
    throw new Error("the approvals payload did not match what this page reads");
  }
  return parsed.data.approvals;
}

/**
 * One decision, posted to the proxy for this request and this verb.
 *
 * No idempotency key is sent: agent-api derives it from the approver, the
 * request and the verb (23c decision B), so a double-clicked Approve replays
 * the stored response instead of filing a second decision.
 */
async function postDecision(
  requestId: string,
  verb: "approve" | "reject",
  body: Record<string, unknown>,
): Promise<void> {
  const response = await fetch(`/api/approvals/${requestId}/${verb}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

  if (response.redirected) {
    window.location.assign(response.url);
    return;
  }
  if (!response.ok) {
    const refused: unknown = await response.json().catch(() => null);
    const code =
      typeof refused === "object" && refused !== null
        ? (refused as Record<string, unknown>).code
        : undefined;
    throw new DecisionRefusedError(code);
  }
}
