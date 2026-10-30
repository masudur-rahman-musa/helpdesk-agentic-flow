import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type ReviewItemsPage,
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

/**
 * One selected review's items, keyed on the review, a page at a time. Each
 * page after the first starts from the cursor the page before handed back.
 */
export function useReviewItems(reviewId: string) {
  return useInfiniteQuery({
    queryKey: queryKeys.reviewItems(reviewId),
    queryFn: ({ pageParam }) => fetchItems(reviewId, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (page: ReviewItemsPage) => page.next_after_item_id ?? null,
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

/** A refused decision. It carries only the typed code; the app picks the sentence. */
export class DecisionRefusedError extends Error {
  readonly code: unknown;

  constructor(code: unknown) {
    super("the decision was refused");
    this.code = code;
  }
}

/**
 * The queue for one filter, or an empty list during the redirect to `/login`.
 * `all` sends no `status`, since the tool's enum has no such word. Errors throw
 * to the queue's own alert.
 */
async function fetchReviews(status: string): Promise<ReviewQueueRow[]> {
  const url = status === "all" ? API.myReviews : `${API.myReviews}?status=${status}`;
  const parsed = await readWire(url, reviewQueueSchema);
  return parsed === null ? [] : parsed.reviews;
}

/**
 * One page of a review's items, or an empty last page during the redirect to
 * `/login`. `after` is only ever a cursor the server handed back.
 */
async function fetchItems(reviewId: string, after: string | null): Promise<ReviewItemsPage> {
  const path = apiPath(API.reviewItems, { reviewId });
  const url = after === null ? path : `${path}?${new URLSearchParams({ after }).toString()}`;
  const parsed = await readWire(url, reviewItemsSchema);
  return parsed ?? { review_id: reviewId, items: [], next_after_item_id: null };
}

/**
 * One decision. No idempotency key: the server makes it from the reviewer, the
 * item and the word, so a double click replays.
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
