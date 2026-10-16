import { NotificationList } from "@/features/notifications/components/notification-list";

/**
 * The `/notifications` route. A thin server wrapper, the same division
 * `/requests`, `/access`, `/approvals` and `/reviews` have: everything it shows
 * comes from the two notification proxies, which the client component reads for
 * itself, so the page has nothing of its own to fetch and
 * `tests/notifications-page.test.tsx` renders the list directly.
 *
 * Reaching this page means the session cookie was present — the middleware's
 * catch-all matcher refuses it otherwise — and the list's own read is
 * what discovers a cookie whose token has since expired. Whose notifications
 * these are is decided by the MCP tool, not by this route: the self-only scope
 * check lives behind the MCP boundary and nowhere else.
 */
export default function NotificationsPage() {
  return <NotificationList />;
}
