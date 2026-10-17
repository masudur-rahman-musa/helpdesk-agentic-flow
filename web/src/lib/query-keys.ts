/**
 * Every TanStack Query key the web app uses, in one place.
 *
 * A feature's `api.ts` reads its keys from here and no file writes a key by
 * hand, so two screens that show the same data share one cache entry, and a
 * refresh after a write reaches every screen that shows it. A key that is a
 * prefix of others (`notifications`, `myReviews`) is what a write refreshes, so
 * the keys built under it must start with it. `tests/query-keys.test.ts` pins
 * the values and the prefixes; `tests/data-calls.test.ts` checks no other file
 * writes a `queryKey:` array.
 */
export const queryKeys = {
  me: ["me"] as const,
  myAccess: ["me", "access"] as const,
  myApprovals: ["me", "approvals"] as const,
  myRequests: ["me", "requests"] as const,
  myReviews: ["me", "reviews"] as const,
  notifications: ["notifications"] as const,
  unreadCount: ["notifications", "unread"] as const,
  chatMessages: ["chat", "messages"] as const,
  killSwitchBoard: ["admin", "killswitch", "board"] as const,
  policiesBoard: ["admin", "policies", "board"] as const,
  rulesBoard: ["admin", "rules", "board"] as const,
  notificationPage: (unreadOnly: boolean) => ["notifications", "page", unreadOnly] as const,
  reviewQueue: (status: string) => ["me", "reviews", status] as const,
  reviewItems: (reviewId: string) => ["reviews", reviewId, "items"] as const,
  auditSearch: (filters: Record<string, string>, pageSize: number, beforeSeq: number | null) =>
    ["admin", "audit", "search", filters, pageSize, beforeSeq] as const,
};
