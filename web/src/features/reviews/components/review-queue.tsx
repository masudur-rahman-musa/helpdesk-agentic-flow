"use client";

import { useState } from "react";

import { ReviewItems } from "@/features/reviews/components/review-items";
import { reviewStatusLabel } from "@/features/reviews/labels";
import { useReviewQueue } from "@/features/reviews/api";
import type { ReviewQueueRow } from "@/features/reviews/schema";

import { StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Alert } from "@/components/ui/alert";
import { formatIsoDate } from "@/lib/date-format";

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

/** The filter's three choices. `all` sends no `status` parameter at all. */
const STATUS_CHOICES = [
  { value: "all", label: "All" },
  { value: "open", label: "Open" },
  { value: "completed", label: "Completed" },
] as const;

export function ReviewQueue() {
  // `open` on first paint (decision D): a completed review is most of the table
  // after two quarters, and the queue a reviewer opens is the work waiting on
  // them. All and Completed are one click away.
  const [status, setStatus] = useState<string>("open");
  const [selected, setSelected] = useState<string | null>(null);

  const reviews = useReviewQueue(status);

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
        <Alert>The review queue could not be read. Try again in a moment.</Alert>
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
        <span>
          {review.due_at === null ? "no due date" : `Due ${formatIsoDate(review.due_at)}`}
        </span>
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
