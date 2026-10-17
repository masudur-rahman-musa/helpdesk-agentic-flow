import { NotificationList } from "@/features/notifications/components/notification-list";

/**
 * The `/notifications` route. A thin server wrapper; the list fetches its own
 * data, and the MCP tool limits it to the caller.
 */
export default function NotificationsPage() {
  return <NotificationList />;
}
