/**
 * Every `/api/...` path the web app serves, in one place. A path with an id
 * marks the spot as `{name}`, and `apiPath` fills it.
 */
export const API = {
  login: "/api/login",
  logout: "/api/logout",
  me: "/api/me",
  myAccess: "/api/me/access",
  myApprovals: "/api/me/approvals",
  myNotifications: "/api/me/notifications",
  myRequests: "/api/me/requests",
  myReviews: "/api/me/reviews",
  myTickets: "/api/me/tickets",
  auditSearch: "/api/admin/audit/search",
  auditVerify: "/api/admin/audit/verify",
  killSwitch: "/api/admin/killswitch",
  settings: "/api/admin/settings",
  policies: "/api/admin/policies",
  policyReingest: "/api/admin/policies/reingest",
  rules: "/api/admin/rules",
  openTickets: "/api/admin/tickets",
  chatStream: "/api/chat/stream",
  chatMessages: "/api/chat/messages",
  chatList: "/api/chat/chats",
  approveRequest: "/api/approvals/{requestId}/approve",
  rejectRequest: "/api/approvals/{requestId}/reject",
  readNotification: "/api/notifications/{notificationId}/read",
  decideReviewItem: "/api/review-items/{itemId}/decide",
  reviewItems: "/api/reviews/{reviewId}/items",
} as const;

/**
 * Fill each `{name}` spot, escaped. A missing value throws, so a path never goes
 * out with a marker or "undefined" in it.
 */
export function apiPath(template: string, params: Record<string, string> = {}): string {
  return template.replace(/\{([^}]+)\}/g, (_, name: string) => {
    const value = params[name];
    if (value === undefined) throw new Error(`apiPath: no value for {${name}} in ${template}`);
    return encodeURIComponent(value);
  });
}
