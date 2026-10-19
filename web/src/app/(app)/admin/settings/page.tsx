import { PageHeader } from "@/components/ui/page-header";
import { ToolStepsSwitch } from "@/features/admin/settings/components/tool-steps-switch";

/**
 * The `/admin/settings` route. Only `platform_admin` can change anything: the
 * tool behind the MCP server checks that, not this page.
 */
export default function AdminSettingsPage() {
  return (
    <section aria-label="Settings" className="flex max-w-3xl flex-col gap-4">
      <PageHeader title="Settings" />
      <ToolStepsSwitch />
    </section>
  );
}
