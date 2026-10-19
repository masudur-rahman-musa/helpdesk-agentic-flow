/**
 * Every TanStack Query key, in one place, so screens showing the same data
 * share a cache entry. A write refreshes a prefix (`notifications`,
 * `myReviews`), so keys under it must start with it.
 */
export const queryKeys = {
  me: ["me"] as const,
  myAccess: ["me", "access"] as const,
  myApprovals: ["me", "approvals"] as const,
  myRequests: ["me", "requests"] as const,
  myReviews: ["me", "reviews"] as const,
  myTickets: ["me", "tickets"] as const,
  notifications: ["notifications"] as const,
  unreadCount: ["notifications", "unread"] as const,
  chatList: ["chat", "list"] as const,
  killSwitchBoard: ["admin", "killswitch", "board"] as const,
  policiesBoard: ["admin", "policies", "board"] as const,
  rulesBoard: ["admin", "rules", "board"] as const,
  openTickets: ["admin", "tickets", "open"] as const,
  chatMessages: (chatId: string) => ["chat", "messages", chatId] as const,
  notificationPage: (unreadOnly: boolean) => ["notifications", "page", unreadOnly] as const,
  reviewQueue: (status: string) => ["me", "reviews", status] as const,
  reviewItems: (reviewId: string) => ["reviews", reviewId, "items"] as const,
  auditSearch: (filters: Record<string, string>, pageSize: number, beforeSeq: number | null) =>
    ["admin", "audit", "search", filters, pageSize, beforeSeq] as const,
};
