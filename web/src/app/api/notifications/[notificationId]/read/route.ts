/**
 * POST /api/notifications/[notificationId]/read: mark one notification read.
 *
 * No body is sent. The id is in the path, and agent-api builds the idempotency
 * key from the reader and the notification, so the browser picks none.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function POST(
  request: Request,
  context: { params: Promise<{ notificationId: string }> },
): Promise<Response> {
  const { notificationId } = await context.params;
  return proxyAgentApi(`/notifications/${notificationId}/read`, { method: "POST" });
}
