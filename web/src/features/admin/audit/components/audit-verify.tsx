"use client";

import { type ChainVerdict, useAuditVerify } from "@/features/admin/audit/api";
import { failureMessage } from "@/lib/failure-messages";
import { WireRefusedError } from "@/lib/wire-read";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Alert } from "@/components/ui/alert";

/**
 * The chain check. It runs on a click, never on load, since it scans the whole
 * log. A mutation, not a query: asking twice should check twice, not hit a
 * cache. It sends no tenant; the server takes that from the token.
 */

/** Says "not checked", never "broken": those are opposite facts. */
export const AUDIT_VERIFY_READ_MESSAGE = "The chain could not be checked. Try again in a moment.";

/**
 * `brokenAt` wins if both seqs arrive. A truncated chain says "missing", since
 * that event no longer exists to look up.
 */
function verdictSentence(verdict: ChainVerdict): string {
  if (verdict.ok) {
    return "The chain is intact.";
  }
  if (verdict.brokenAt !== null) {
    return `The chain is broken at event ${verdict.brokenAt}.`;
  }
  if (verdict.truncatedAt !== null) {
    return `The chain is missing event ${verdict.truncatedAt} and everything after it.`;
  }
  return "The chain did not verify.";
}

/** Shown on every verdict: an unanchored check proves less. */
function anchorLine(anchoredSeq: number | null): string {
  if (anchoredSeq === null) {
    return "No head has been anchored yet, so a removed tail would not show here.";
  }
  return `Checked against the anchored head, event ${anchoredSeq}.`;
}

export function AuditVerify() {
  const verify = useAuditVerify();

  const verdict = verify.isSuccess ? verify.data : null;
  const refusal = verify.isError ? errorMessage(verify.error) : null;

  return (
    <section aria-label="Chain integrity" className="flex max-w-5xl flex-col gap-3">
      <h2 className="text-base font-semibold">Chain integrity</h2>

      <div>
        <Button type="button" disabled={verify.isPending} onClick={() => verify.mutate()}>
          Verify integrity
        </Button>
      </div>

      {refusal !== null ? <Alert>{refusal}</Alert> : null}

      {verdict !== null ? (
        <Card className="flex flex-col gap-1 p-4">
          {/* A broken chain is a verdict, not an alert: tampering is not an outage. */}
          <p className="text-sm">{verdictSentence(verdict)}</p>
          <p className="text-sm text-muted-foreground">{anchorLine(verdict.anchoredSeq)}</p>
        </Card>
      ) : null}
    </section>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return AUDIT_VERIFY_READ_MESSAGE;
}
