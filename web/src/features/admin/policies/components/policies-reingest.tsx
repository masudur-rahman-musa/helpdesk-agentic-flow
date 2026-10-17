"use client";

import { Button } from "@/components/ui/button";
import { failureMessage } from "@/lib/failure-messages";
import { ReingestRefusedError, usePolicyReingest } from "@/features/admin/policies/api";

/**
 * Re-ingest one policy version from the ingest host. Each control has its own
 * pending state, since one ingest can take most of a minute. The body sends no
 * tenant and no digest: the server takes the tenant from the token and reads
 * the file itself. The armed flag lives in the table, so a click elsewhere in
 * the same document can clear it.
 */

/** Says the file did not land, which is what the admin needs to know. */
export const REINGEST_FAILED_MESSAGE =
  "The re-ingest could not be completed. Try again in a moment.";

/** The fix is to put the file on the host, not to retry, so say so. */
export const REINGEST_NOT_FOUND_MESSAGE = "No file for that version was found on the ingest host.";

/** The shared sentence is about chat messages. */
export const REINGEST_VALIDATION_MESSAGE =
  "That version could not be re-ingested. Check the document and try again.";

/**
 * The count shows whether the file gave any text, and re-ingesting an older
 * version makes it live, so the line says which one is live now.
 */
export function REINGEST_CREATED_TEXT(chunkCount: number, isCurrent: boolean): string {
  const chunks = `${chunkCount} ${chunkCount === 1 ? "chunk" : "chunks"}`;
  const live = isCurrent ? "This version is now live." : "This version is not the live one.";
  return `Re-ingested: ${chunks} written. ${live}`;
}

/** Without this, an unchanged file looks like a click that did nothing. */
export const REINGEST_UNCHANGED_TEXT =
  "The file on the ingest host has not changed. Nothing was re-ingested.";

export function PoliciesReingest({
  slug,
  version,
  isCurrent,
  armed,
  onArmedChange,
  onChanged,
}: {
  slug: string;
  version: string;
  isCurrent: boolean;
  armed: boolean;
  onArmedChange: (armed: boolean) => void;
  onChanged: () => void;
}) {
  const reingest = usePolicyReingest(slug, version, onChanged);

  const refusal = reingest.isError ? reingestRefusal(reingest.error) : null;
  const result = reingest.isSuccess ? reingest.data : null;
  const name = armed
    ? `Confirm re-ingest ${slug} ${version}, replacing the live version`
    : `Re-ingest ${slug} ${version}`;

  /**
   * Only a version that is not live needs a second press, since re-ingesting
   * it makes it live. Any other click clears the arm first.
   */
  function onClick(): void {
    if (!isCurrent && !armed) {
      onArmedChange(true);
      return;
    }
    onArmedChange(false);
    reingest.mutate();
  }

  return (
    <div className="flex flex-col gap-1">
      <Button type="button" aria-label={name} disabled={reingest.isPending} onClick={onClick}>
        {armed ? "Confirm re-ingest" : "Re-ingest"}
      </Button>

      {refusal !== null ? (
        <p role="alert" className="text-xs">
          {refusal}
        </p>
      ) : null}

      {result !== null ? (
        <p role="status" className="text-xs text-muted-foreground">
          {result.created
            ? REINGEST_CREATED_TEXT(result.chunk_count, result.is_current)
            : REINGEST_UNCHANGED_TEXT}
        </p>
      ) : null}
    </div>
  );
}

function reingestRefusal(error: unknown): string {
  if (!(error instanceof ReingestRefusedError)) {
    return REINGEST_FAILED_MESSAGE;
  }
  if (error.code === "NOT_FOUND") {
    return REINGEST_NOT_FOUND_MESSAGE;
  }
  if (error.code === "VALIDATION") {
    return REINGEST_VALIDATION_MESSAGE;
  }
  return failureMessage(error.code);
}
