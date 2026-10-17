"use client";

import { PolicyVersionsTable } from "@/features/admin/policies/components/policy-versions-table";
import { failureMessage } from "@/lib/failure-messages";
import {
  NO_LIVE_VERSION_TEXT,
  POLICIES_EMPTY_TEXT,
  POLICIES_READ_MESSAGE,
} from "@/features/admin/policies/format";
import { usePoliciesBoard, useRefreshPoliciesBoard } from "@/features/admin/policies/api";
import type { PolicyDocument } from "@/features/admin/policies/schema";
import { WireRefusedError } from "@/lib/wire-read";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Alert } from "@/components/ui/alert";

/**
 * The policy documents board. Rows keep the server's order. A failed read
 * shows an alert, never "nothing ingested", which an admin would act on.
 */

export function PoliciesBoard() {
  const board = usePoliciesBoard();

  const onChanged = useRefreshPoliciesBoard();

  const refusal = board.isError ? errorMessage(board.error) : null;
  const documents = board.isSuccess ? board.data : null;

  return (
    <section aria-label="Policies" className="flex max-w-5xl flex-col gap-6">
      <PageHeader title="Policy documents" />

      {refusal !== null ? <Alert>{refusal}</Alert> : null}

      {documents !== null && documents.length === 0 ? (
        <EmptyState title={POLICIES_EMPTY_TEXT} />
      ) : null}

      {documents !== null
        ? documents.map((entry) => (
            <DocumentSection key={entry.slug} policy={entry} onChanged={onChanged} />
          ))
        : null}
    </section>
  );
}

/** A document with no versions shows only its heading. */
function DocumentSection({ policy, onChanged }: { policy: PolicyDocument; onChanged: () => void }) {
  const hasVersions = policy.versions.length > 0;
  const hasLive = policy.versions.some((entry) => entry.is_current);

  return (
    <section aria-label={policy.slug} className="flex flex-col gap-2">
      <h2 className="text-base font-medium">{policy.title}</h2>

      {hasVersions && !hasLive ? (
        <p className="text-sm text-muted-foreground">{NO_LIVE_VERSION_TEXT}</p>
      ) : null}

      {hasVersions ? <PolicyVersionsTable policy={policy} onChanged={onChanged} /> : null}
    </section>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return POLICIES_READ_MESSAGE;
}
