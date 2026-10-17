import { AuditSearch } from "@/features/admin/audit/components/audit-search";
import { AuditVerify } from "@/features/admin/audit/components/audit-verify";

/**
 * The `/admin/audit` route: the audit log search and the chain check, side by
 * side. They share no state, so each is its own component and fetches for
 * itself. Access is checked by the tool behind the MCP server, not here, so the
 * nav shows this page to everyone.
 */
export default function AdminAuditPage() {
  return (
    <div className="flex flex-col gap-8">
      <AuditSearch />
      <AuditVerify />
    </div>
  );
}
