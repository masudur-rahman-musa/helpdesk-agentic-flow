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

/**
 * One selected review's items, read only once that review is selected.
 *
 * Its own query, keyed on the review, so selecting a second review replaces
 * this list rather than adding to it: two lists on screen would put a grant
 * from another scope beside the Keep and Revoke controls of this one, and the
 * tool would happily file the decision the reviewer meant for the other row.
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
