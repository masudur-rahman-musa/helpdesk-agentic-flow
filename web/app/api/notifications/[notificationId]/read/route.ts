/**
 * POST /api/notifications/[notificationId]/read — one read stamp.
 *
 * The same `proxyAgentApi` seam every other call uses, so the bearer comes off
 * the httpOnly cookie and an expired session clears it and lands the browser on
 * `/login`. A stamp is a mutation, which is exactly why it goes through the seam
 * rather than around it: agent-api derives the idempotency key from the verified
 * reader and the notification, and the MCP tool enforces RBAC, the self-only
 * scope check, the kill switch and the audit event.
 *
 * The path is `/read` and not `/mark-read` (decision C): the resource is the
 * notification and the sub-path is the state being set, the shape
 * `/approvals/[requestId]/approve` and `/review-items/[itemId]/decide` already
 * use next door. `mark-read` would name the TOOL, which is an implementation
 * detail of the far side of the MCP boundary.
 *
 * NO BODY is sent, and no content type with it (decision B): there is nothing
 * for the browser to say beyond which row it opened, the id is in the path, and
 * the browser sends no idempotency key at all — one it could choose would be
 * `IDEMPOTENCY_CONFLICT` taken by accident across two notifications. The id
 * comes from this route's own `params` rather than from a fixed value, so a
 * click clears the row it was made on.
 */

import { proxyAgentApi } from "@/lib/agent-api";

export async function POST(
  request: Request,
  context: { params: Promise<{ notificationId: string }> },
): Promise<Response> {
  const { notificationId } = await context.params;
  return proxyAgentApi(`/notifications/${notificationId}/read`, { method: "POST" });
}
