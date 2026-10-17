"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { failureMessage } from "@/features/chat/errors";
import {
  reviewFlagLabel,
  reviewItemDecisionLabel,
  reviewStatusLabel,
} from "@/features/reviews/labels";
import {
  type ReviewItem,
  type ReviewQueueRow,
  reviewItemsSchema,
  reviewQueueSchema,
} from "@/features/reviews/schema";
import { API, apiPath } from "@/lib/api-paths";
import { readWire } from "@/lib/wire-read";

import { StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

/**
 * The reviews waiting on this reviewer, from `GET /api/me/reviews`, the items
 * of the one they selected, and the keep/revoke decision on each.
 *
 * A client component doing its own reads and its own mutations through TanStack
 * Query (§2, 23d decision A), exactly `ApprovalQueue`'s shape. Both READS go
 * through `lib/wire-read.ts`, which answers `null` while the browser
 * follows the proxy's redirect to `/login` (22b decision E) and is mapped here
 * to the empty list; the decision keeps its own check of it, because `readWire`
 * is a GET and because a review queue sits open on a screen for many minutes
 * before anyone clicks Keep. All THREE call sites still follow that redirect,
 * which is what `tests/reviews-page.test.tsx`'s three `an expired session`
 * cases pin, one per call site.
 *
 * Every act crosses 23h's proxies, so a project rule holds: the decision is a
 * typed MCP tool behind RBAC, the kill switch, the scope rules and the audit
 * event, and this component is the surface. Nothing here decides anything —
 * which reviews the caller can see, which items belong to a review, what a
 * Revoke does to the grant behind it and whether the review completed all come
 * from the tools; this renders and labels.
 *
 * Exactly ONE review's items are read at a time (§1): the selected id is the
 * only page-level state, and the item list is a child mounted under that row
 * alone. A child mounted per row would turn a queue of fifty into fifty MCP
 * round trips on first paint and still look right, which
 * `::reads no items at all until a review is selected` is what catches.
 *
 * A failed read THROWS rather than answering an empty list, so an alert and an
 * empty state are mutually exclusive by construction: `No reviews are waiting
 * for you.` over a broken proxy is the sentence a reviewer acts on by closing
 * the tab on a quarter's worth of unexamined access. Pinned from both sides by
 * `::renders the named empty queue with no list and no alert` and the two
 * `renders the queue alert` rows, and again one layer down for the items.
 *
 * Accessible names: the region is `Reviews`, the rows are the list `Review
 * queue`, the filter is the select `Status`, and a selected review's items are
 * the list `Items in review`. Each row's control carries the period (`Show
 * items for {period}`) and each item's two carry the role and the target
 * (`Keep {role_name} for {target_display_name}`), because two rows otherwise
 * carry several controls with one name.
 *
 * Dates render as the ISO prefix, never `toLocaleDateString`, which answers a
 * different day either side of a date line and is asserted absent by
 * `::renders one row per review...`.
 */

/**
 * The `VALIDATION` sentence on this surface, and only on this surface
 * (decision E).
 *
 * `FAILURE_MESSAGES.VALIDATION` is "That message could not be sent.", which is
 * about a chat message and says nothing true about a refused decision; 22b's
 * `tests/chat-schema.test.ts` pins every string in that map verbatim, so
 * widening the shared map is not available. Exactly ONE code is overridden —
 * every other sentence still comes from the shared map, so no second opinion
 * about `FORBIDDEN` or `TOOL_DISABLED` can appear here. Pinned by
 * `tests/reviews-page.test.tsx::pins the VALIDATION override sentence exactly`.
 */
export const REVIEW_VALIDATION_MESSAGE =
  "That decision could not be filed. Reload the page and try again.";

/** The filter's three choices. `all` sends no `status` parameter at all. */
const STATUS_CHOICES = [
  { value: "all", label: "All" },
  { value: "open", label: "Open" },
  { value: "completed", label: "Completed" },
] as const;

/** The prefix every queue key shares, so a decision invalidates any filter. */
const QUEUE_KEY_PREFIX = ["me", "reviews"] as const;

export function ReviewQueue() {
  // `open` on first paint (decision D): a completed review is most of the table
  // after two quarters, and the queue a reviewer opens is the work waiting on
  // them. All and Completed are one click away.
  const [status, setStatus] = useState<string>("open");
  const [selected, setSelected] = useState<string | null>(null);

  const reviews = useQuery({
    queryKey: [...QUEUE_KEY_PREFIX, status],
    queryFn: () => fetchReviews(status),
  });

  return (
    <section aria-label="Reviews" className="flex max-w-3xl flex-col gap-4">
      <PageHeader title="Reviews" />

      <select
        aria-label="Status"
        value={status}
        onChange={(event) => setStatus(event.target.value)}
        className="w-48 rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {STATUS_CHOICES.map((choice) => (
          <option key={choice.value} value={choice.value}>
            {choice.label}
          </option>
        ))}
      </select>

      {reviews.isError ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          The review queue could not be read. Try again in a moment.
        </p>
      ) : null}

      {reviews.isSuccess && reviews.data.length === 0 ? (
        <EmptyState title="No reviews are waiting for you." />
      ) : null}

      {reviews.isSuccess && reviews.data.length > 0 ? (
        <ul aria-label="Review queue" className="flex flex-col gap-3">
          {reviews.data.map((review) => (
            <ReviewRow
              key={review.review_id}
              review={review}
              selected={review.review_id === selected}
              onSelect={() => setSelected(review.review_id)}
            />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function ReviewRow({
  review,
  selected,
  onSelect,
}: {
  review: ReviewQueueRow;
  selected: boolean;
  onSelect: () => void;
}) {
  // Exactly one of the two scope pairs is non-null on any row (`access_reviews`
  // allows one scope id), so a row reading `department_name` unconditionally
  // leaves a blank cell on every system review.
  const scopeName = review.department_name ?? review.system_name ?? "unknown scope";

  return (
    <Card as="li" className="flex flex-col gap-2 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{scopeName}</span>
        <span className="text-muted-foreground">{review.period}</span>
        <StatusBadge status={review.status}>{reviewStatusLabel(review.status)}</StatusBadge>
      </div>

      <div className="flex flex-wrap gap-2 text-muted-foreground">
        <span>{review.due_at === null ? "no due date" : `Due ${review.due_at.slice(0, 10)}`}</span>
        <span>{review.reviewer_display_name}</span>
        <span>
          {review.decided_count} of {review.item_count} decided
        </span>
      </div>

      <div>
        <Button type="button" aria-label={`Show items for ${review.period}`} onClick={onSelect}>
          Show items
        </Button>
      </div>

      {selected ? <ReviewItems reviewId={review.review_id} /> : null}
    </Card>
  );
}

/**
 * One selected review's items, read only once that review is selected.
 *
 * Its own query, keyed on the review, so selecting a second review replaces
 * this list rather than adding to it: two lists on screen would put a grant
 * from another scope beside the Keep and Revoke controls of this one, and the
 * tool would happily file the decision the reviewer meant for the other row.
 */
function ReviewItems({ reviewId }: { reviewId: string }) {
  const items = useQuery({
    queryKey: ["reviews", reviewId, "items"],
    queryFn: () => fetchItems(reviewId),
  });

  if (items.isError) {
    return (
      <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
        That review&apos;s items could not be read. Try again in a moment.
      </p>
    );
  }

  if (!items.isSuccess) {
    return null;
  }

  if (items.data.length === 0) {
    return <p className="text-sm text-muted-foreground">This review has no items.</p>;
  }

  return (
    <ul aria-label="Items in review" className="flex flex-col gap-3">
      {items.data.map((item) => (
        <ItemRow key={item.item_id} item={item} reviewId={reviewId} />
      ))}
    </ul>
  );
}

function ItemRow({ item, reviewId }: { item: ReviewItem; reviewId: string }) {
  return (
    <Card as="li" className="flex flex-col gap-2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{reviewFlagLabel(item.flag)}</span>
        <span>{item.target_display_name}</span>
      </div>

      <div className="flex flex-wrap gap-2 text-muted-foreground">
        <span>{item.role_name}</span>
        <span>{item.system_name}</span>
        <span>{item.privilege}</span>
      </div>

      <div className="flex flex-wrap gap-2 text-muted-foreground">
        <span>Granted {item.granted_at.slice(0, 10)}</span>
        <span>
          {item.expires_at === null ? "no expiry" : `Expires ${item.expires_at.slice(0, 10)}`}
        </span>
      </div>

      {item.decision === null ? (
        <DecideControls item={item} reviewId={reviewId} />
      ) : (
        <StatusBadge status={item.decision} className="self-start">
          {reviewItemDecisionLabel(item.decision)}
        </StatusBadge>
      )}
    </Card>
  );
}

/**
 * The two acts, per ITEM.
 *
 * Per item on purpose. One in-flight flag for the whole list would grey out
 * every control while a single grant is being decided, which a reviewer holding
 * forty items pays for forty times; no flag at all leaves the Revoke beside an
 * in-flight Keep live, so the opposite decision can be filed on the same item.
 * The guarantee against a double-filed decision is not this: it is the
 * server-derived idempotency key (23h), and these disabled controls are a
 * courtesy on top of it.
 *
 * A success invalidates BOTH the item query and the queue: the progress counts
 * live on the queue payload, so invalidating the items alone leaves `1 of 4
 * decided` sitting under an item the reviewer just decided. Nothing is decided
 * optimistically — `review_complete` and the item's new decision are only ever
 * what the refetch answers.
 */
function DecideControls({ item, reviewId }: { item: ReviewItem; reviewId: string }) {
  const queryClient = useQueryClient();

  const decide = useMutation({
    mutationFn: (decision: "keep" | "revoke") => postDecision(item.item_id, decision),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["reviews", reviewId, "items"] });
      await queryClient.invalidateQueries({ queryKey: QUEUE_KEY_PREFIX });
    },
  });

  const refusal = decisionRefusal(decide.error);

  return (
    <div className="flex flex-col gap-2">
      {refusal !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {refusal}
        </p>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          aria-label={`Keep ${item.role_name} for ${item.target_display_name}`}
          disabled={decide.isPending}
          onClick={() => decide.mutate("keep")}
        >
          Keep
        </Button>
        <Button
          type="button"
          aria-label={`Revoke ${item.role_name} for ${item.target_display_name}`}
          disabled={decide.isPending}
          onClick={() => decide.mutate("revoke")}
        >
          Revoke
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
  return code === "VALIDATION" ? REVIEW_VALIDATION_MESSAGE : failureMessage(code);
}

/**
 * The queue for one filter, or the empty list while the browser follows the
 * proxy's redirect to `/login`. `all` sends no `status` parameter at all rather
 * than a word the tool's enum has no member for.
 *
 * A refused read throws a `WireRefusedError` and this page dresses it with
 * nothing (24ia decision B): the queue's own alert is what a reviewer sees for a
 * typed 403 as well as for a bodyless 500, and the per-code sentences on this
 * surface stay the DECISION's alone. Pinned by
 * `tests/reviews-page.test.tsx::a 403 FORBIDDEN on the queue renders the queue
 * alert, not the code's sentence`.
 */
async function fetchReviews(status: string): Promise<ReviewQueueRow[]> {
  const url = status === "all" ? API.myReviews : `${API.myReviews}?status=${status}`;
  const parsed = await readWire(url, reviewQueueSchema);
  return parsed === null ? [] : parsed.reviews;
}

/**
 * The items of one review, or the empty list while the browser follows the
 * proxy's redirect to `/login`. The review id is a path segment and never a
 * query parameter, pinned by `tests/reviews-page.test.tsx::reads
 * /api/reviews/{review_id}/items with an empty query string`.
 *
 * Same dressing as the queue read: the item alert answers a typed 403 and a
 * bodyless 500 alike (`::a 403 FORBIDDEN on the items renders the item alert,
 * not the code's sentence`).
 */
async function fetchItems(reviewId: string): Promise<ReviewItem[]> {
  const parsed = await readWire(apiPath(API.reviewItems, { reviewId }), reviewItemsSchema);
  return parsed === null ? [] : parsed.items;
}

/**
 * One decision, posted to the proxy for this item.
 *
 * No idempotency key is sent: agent-api derives it from the verified reviewer,
 * the item and the word (23h), so a double-clicked Keep replays the stored
 * response instead of filing a second decision. The only string this page sends
 * is the tool's own enum, chosen from a fixed control.
 */
async function postDecision(itemId: string, decision: "keep" | "revoke"): Promise<void> {
  const response = await fetch(apiPath(API.decideReviewItem, { itemId }), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ decision }),
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
