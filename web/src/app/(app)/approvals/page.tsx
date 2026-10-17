import { ApprovalQueue } from "@/features/approvals/components/approval-queue";

/**
 * The `/approvals` route. A thin server wrapper; the queue fetches its own
 * data. Who may approve is checked by the tool behind the MCP server, not here.
 */
export default function ApprovalsPage() {
  return <ApprovalQueue />;
}
