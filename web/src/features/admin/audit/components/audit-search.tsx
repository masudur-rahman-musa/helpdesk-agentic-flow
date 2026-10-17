"use client";

import { useState } from "react";

import { AUDIT_CSV_MIME, csvFilename, toCsv } from "@/features/admin/audit/csv";
import { DEFAULT_PAGE_SIZE, isInvertedRange } from "@/features/admin/audit/filters";
import { type AppliedFilters, useAuditSearch } from "@/features/admin/audit/api";
import type { AuditEventRow } from "@/features/admin/audit/schema";
import { failureMessage } from "@/lib/failure-messages";
import { WireRefusedError } from "@/lib/wire-read";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Alert } from "@/components/ui/alert";
import { AuditEventTable } from "@/features/admin/audit/components/audit-event-table";
import {
  AuditFilterForm,
  type Draft,
  EMPTY_DRAFT,
  filtersOf,
} from "@/features/admin/audit/components/audit-filter-form";

/**
 * The searchable audit log. Filters, page size and cursor are all in the query
 * key, so stepping back is a cache hit and a cursor never pages another search.
 * An empty control sends no parameter: `event_type=` would match nothing.
 * A failed read shows an alert, never "no events": an auditor would read that
 * as "nothing happened".
 */

/**
 * Only `VALIDATION` gets its own sentence here: the shared one talks about a
 * chat message. Every other code uses the shared text.
 */
export const AUDIT_VALIDATION_MESSAGE =
  "That search could not be run. Check the filters and try again.";

/** Checked in the browser, so a bad range never costs a round trip. */
export const AUDIT_RANGE_MESSAGE = "The From date is after the To date.";

const AUDIT_READ_MESSAGE = "The audit log could not be read. Try again in a moment.";

export function AuditSearch() {
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [applied, setApplied] = useState<AppliedFilters>({});
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  // Cursors of pages walked past, newest last; empty means the newest page.
  const [stack, setStack] = useState<number[]>([]);
  const [rangeError, setRangeError] = useState<string | null>(null);

  const beforeSeq = stack.length === 0 ? null : (stack[stack.length - 1] ?? null);

  const search = useAuditSearch(applied, pageSize, beforeSeq);

  function commit(): void {
    if (isInvertedRange(draft.from ?? "", draft.to ?? "")) {
      setRangeError(AUDIT_RANGE_MESSAGE);
      return;
    }
    setRangeError(null);
    setApplied(filtersOf(draft));
    setStack([]);
  }

  function choosePageSize(value: number): void {
    setPageSize(value);
    // A cursor from one page size points into a window the other never asked for.
    setStack([]);
  }

  const refusal = search.isError ? errorMessage(search.error) : null;
  const events = search.isSuccess ? search.data.events : [];

  return (
    <section aria-label="Audit log" className="flex max-w-5xl flex-col gap-4">
      <PageHeader title="Audit log" />

      <AuditFilterForm
        draft={draft}
        setDraft={setDraft}
        pageSize={pageSize}
        onPageSize={choosePageSize}
        onCommit={commit}
      />

      {rangeError !== null ? <Alert>{rangeError}</Alert> : null}

      {refusal !== null ? <Alert>{refusal}</Alert> : null}

      {search.isSuccess && events.length === 0 ? (
        <EmptyState title="No events match that search." />
      ) : null}

      {search.isSuccess && events.length > 0 ? <AuditEventTable events={events} /> : null}

      {search.isSuccess ? (
        <div className="flex flex-wrap gap-2">
          {events.length > 0 ? (
            <Button type="button" onClick={() => downloadCsv(events)}>
              Export CSV
            </Button>
          ) : null}
          {stack.length > 0 ? (
            <Button type="button" onClick={() => setStack((current) => current.slice(0, -1))}>
              Newer
            </Button>
          ) : null}
          {search.data.nextBeforeSeq !== null ? (
            <Button
              type="button"
              onClick={() => {
                const cursor = search.data.nextBeforeSeq;
                if (cursor !== null) {
                  setStack((current) => [...current, cursor]);
                }
              }}
            >
              Older
            </Button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

/**
 * Saves exactly the rows on screen, with no second read. The URL is revoked
 * right away, or each export would hold its file in memory for good.
 */
function downloadCsv(events: AuditEventRow[]): void {
  const url = URL.createObjectURL(new Blob([toCsv(events)], { type: AUDIT_CSV_MIME }));
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = csvFilename(events);
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return error.code === "VALIDATION" ? AUDIT_VALIDATION_MESSAGE : failureMessage(error.code);
  }
  return AUDIT_READ_MESSAGE;
}
