/**
 * Every `/api/...` path the web app serves, in one place.
 *
 * Callers read a path from `API` instead of writing the string by hand. A path
 * with an id in it is a template with the spot marked `{name}`, and `apiPath`
 * fills it. `tests/api-paths.test.ts` checks the list against the route files
 * under `src/app/api/` both ways, so a renamed, missing or unlisted route fails.
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
  auditSearch: "/api/admin/audit/search",
  auditVerify: "/api/admin/audit/verify",
  killSwitch: "/api/admin/killswitch",
  policies: "/api/admin/policies",
  policyReingest: "/api/admin/policies/reingest",
  rules: "/api/admin/rules",
  chatStream: "/api/chat/stream",
  chatMessages: "/api/chat/messages",
  approveRequest: "/api/approvals/{requestId}/approve",
  rejectRequest: "/api/approvals/{requestId}/reject",
  readNotification: "/api/notifications/{notificationId}/read",
  decideReviewItem: "/api/review-items/{itemId}/decide",
  reviewItems: "/api/reviews/{reviewId}/items",
} as const;

/**
 * Fill each `{name}` spot in `template` with `params[name]`, escaped. A spot
 * with no value throws, so a path never goes out with a marker or "undefined"
 * in it (`::apiPath refuses a missing id`).
 */
export function apiPath(template: string, params: Record<string, string> = {}): string {
  return template.replace(/\{([^}]+)\}/g, (_, name: string) => {
    const value = params[name];
    if (value === undefined) throw new Error(`apiPath: no value for {${name}} in ${template}`);
    return encodeURIComponent(value);
  });
}
