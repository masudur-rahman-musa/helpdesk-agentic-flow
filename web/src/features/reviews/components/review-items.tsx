"use client";

import { failureMessage } from "@/lib/failure-messages";
import { reviewFlagLabel, reviewItemDecisionLabel } from "@/features/reviews/labels";
import { DecisionRefusedError, useDecideReviewItem, useReviewItems } from "@/features/reviews/api";
import type { ReviewItem } from "@/features/reviews/schema";

import { StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Alert } from "@/components/ui/alert";
import { formatIsoDate } from "@/lib/date-format";

/**
 * The shared `VALIDATION` sentence is about chat messages, so this page swaps in
 * its own for that one code only.
 */
export const REVIEW_VALIDATION_MESSAGE =
  "That decision could not be filed. Reload the page and try again.";

/**
 * One selected review's items. Keyed on the review, so picking another review
 * replaces the list: two lists side by side could get a decision filed on the
 * wrong grant.
 */
export function ReviewItems({ reviewId }: { reviewId: string }) {
  const items = useReviewItems(reviewId);

  if (items.isError) {
    return <Alert>That review&apos;s items could not be read. Try again in a moment.</Alert>;
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
        <span>Granted {formatIsoDate(item.granted_at)}</span>
        <span>
          {item.expires_at === null ? "no expiry" : `Expires ${formatIsoDate(item.expires_at)}`}
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
 * Keep and revoke, per item. A list-wide flag would lock every row; no flag
 * would leave Revoke live beside a running Keep. The server's idempotency key is
 * the real guard. A success refreshes the items and the queue, since the
 * progress counts live on the queue.
 */
function DecideControls({ item, reviewId }: { item: ReviewItem; reviewId: string }) {
  const decide = useDecideReviewItem(item.item_id, reviewId);

  const refusal = decisionRefusal(decide.error);

  return (
    <div className="flex flex-col gap-2">
      {refusal !== null ? <Alert>{refusal}</Alert> : null}

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

/** The sentence for a refused decision, or `null` while nothing is refused. */
function decisionRefusal(error: unknown): string | null {
  if (error === null || error === undefined) {
    return null;
  }
  const code = error instanceof DecisionRefusedError ? error.code : undefined;
  return code === "VALIDATION" ? REVIEW_VALIDATION_MESSAGE : failureMessage(code);
}
