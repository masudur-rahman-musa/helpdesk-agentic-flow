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
 * The reviews waiting on this reviewer, the items of the selected one, and
 * keep or revoke on each. Every action goes through a typed server tool; this
 * only renders. Only one review's items load at a time, so a long queue is not
 * one server call per row. A failed read shows an alert, never the empty state,
 * so nobody walks away from unchecked access.
 */

/** The filter's three choices. `all` sends no `status` parameter at all. */
const STATUS_CHOICES = [
  { value: "all", label: "All" },
  { value: "open", label: "Open" },
  { value: "completed", label: "Completed" },
] as const;

export function ReviewQueue() {
  // Starts on `open`: completed reviews soon fill the table, and the open ones
  // are the work.
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
  // Only one scope pair is set on a row, so pick the one that is there.
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
