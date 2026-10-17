import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type ReviewItem,
  type ReviewQueueRow,
  reviewItemsSchema,
  reviewQueueSchema,
} from "@/features/reviews/schema";
import { API, apiPath } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** The review queue for one status filter. */
export function useReviewQueue(status: string) {
  return useQuery({
    queryKey: queryKeys.reviewQueue(status),
    queryFn: () => fetchReviews(status),
  });
}

/** One selected review's items, keyed on the review. */
export function useReviewItems(reviewId: string) {
  return useQuery({
    queryKey: queryKeys.reviewItems(reviewId),
    queryFn: () => fetchItems(reviewId),
  });
}

/**
 * Keep or revoke one item. A success re-reads BOTH the item list and every
 * queue filter (the shared `myReviews` prefix): the progress counts live on the
 * queue payload.
 */
export function useDecideReviewItem(itemId: string, reviewId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (decision: "keep" | "revoke") => postDecision(itemId, decision),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.reviewItems(reviewId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.myReviews });
    },
  });
}

/**
 * A decision the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
export class DecisionRefusedError extends Error {
  readonly code: unknown;

  constructor(code: unknown) {
    super("the decision was refused");
    this.code = code;
  }
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
