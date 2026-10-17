import { ReviewQueue } from "@/features/reviews/components/review-queue";

/**
 * The `/reviews` route. A thin server wrapper; the queue fetches its own data.
 * Who may review what is checked by the tool behind the MCP server.
 */
export default function ReviewsPage() {
  return <ReviewQueue />;
}
