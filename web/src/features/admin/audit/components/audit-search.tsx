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
 * The searchable audit log, from `GET /api/admin/audit/search`.
 *
 * A client component doing its own read through TanStack Query (§2, 23d
 * decision A), `ReviewQueue`'s and `ApprovalQueue`'s shape, reading through
 * `lib/wire-read.ts`, which owns the redirect, the status, the the design
 * vocabulary check and the `safeParse` for every board that talks to `app/api/`
 *. What stays here is the sentence, `AUDIT_VALIDATION_MESSAGE`
 * included. Pinned by `tests/audit-search-page.test.tsx::follows the proxy's
 * redirect to /login instead of rendering the log`.
 *
 * Everything here is a READ, so a project rule has nothing to bite on; a project rule
 * is 24a's and the tool's — this page sends no tenant and no user, and could not
 * be believed if it did. Nothing here decides anything either: which events this
 * caller may see, how they are ordered, how many rows one call may scan and
 * where the next page starts all come from 13b's tool. This composes a query
 * string, labels nothing (decision B) and renders.
 *
 * Three pieces of page-level state and no more: the APPLIED filters (what the
 * last `Search` committed, never the keystrokes), the chosen page size and the
 * cursor stack. All three are in the query key — `["admin", "audit", "search",
 * filters, pageSize, beforeSeq]` — so TanStack Query does the caching, stepping
 * back a page is a cache hit, and a cursor from one filter can never page
 * another. The page size is in the key because it is in the REQUEST: a key
 * without it answers the 50-row page from cache and the auditor's choice of 200
 * fires no read at all. Pinned by `::committing a filter after paging clears the
 * cursor stack`, `::choosing 200 refetches the log with limit=200` and
 * `::clears the cursor stack when the page size changes too`.
 *
 * An empty control sends NO parameter rather than an empty one: `event_type=` is
 * a filter nothing matches, so a form submitting its empty siblings would answer
 * every filtered search with an empty table. Pinned as the exact query string by
 * `::reads the search proxy with limit=50 and nothing else` and per control by
 * `::$control sends $param and no other parameter`.
 *
 * `Export CSV` is offered exactly when the page on screen holds at least one
 * row: not while the first read is in flight, not on an empty
 * page and not over a failed read, so a header-only file named for a range it
 * does not hold is never produced at all. The bytes are `lib/audit-csv.ts`'s
 * and the four absences are pinned by `tests/audit-export.test.tsx`'s four
 * `::offers ...` cases.
 *
 * A failed read THROWS rather than answering an empty page, so the alert and the
 * empty state are mutually exclusive by construction: `No events match that
 * search.` over a broken proxy is the one sentence on this page an auditor acts
 * on by concluding nothing happened. Pinned from both sides by `::renders the
 * named empty state with no table and no alert` and the three `$name renders the
 * read alert` rows.
 *
 * Accessible names: the region is `Audit log`, the results are the table `Audit
 * events`, the controls are `Event type`, `Subject type`, `Subject id`, `Actor`,
 * `From`, `To`, `Page size`, `Search`, `Export CSV`, `Older` and `Newer`, and
 * each row's
 * disclosure carries the event's `seq` (`Payload for event {seq}`) because fifty
 * rows otherwise hold fifty identically named controls. That name is an
 * `aria-label` on the `<details>` itself: `details` maps to role `group`, whose
 * name comes from the author and never from its `<summary>` (the project notes,
 * a project rule).
 */

/**
 * The `VALIDATION` sentence on this surface, and only on this surface
 * (decision H).
 *
 * `FAILURE_MESSAGES.VALIDATION` is "That message could not be sent.", which is
 * about a chat message and says nothing true about a refused search; 22b's
 * `tests/chat-schema.test.ts` pins every string in that map verbatim, so
 * widening the shared map is not available. Exactly ONE code is overridden —
 * every other sentence still comes from the shared map, so no second opinion
 * about `FORBIDDEN` or `TOOL_DISABLED` can appear here. Pinned by
 * `tests/audit-search-page.test.tsx::pins the VALIDATION override sentence
 * exactly`.
 */
export const AUDIT_VALIDATION_MESSAGE =
  "That search could not be run. Check the filters and try again.";

/**
 * What an inverted range says, in the browser, before any read goes out: the
 * tool would answer `InvalidTimeRangeError` as a `VALIDATION` refusal, and a
 * round trip is a worse answer than a form that will not submit. Pinned by
 * `::pins the inverted-range sentence exactly`.
 */
export const AUDIT_RANGE_MESSAGE = "The From date is after the To date.";

/** The sentence for a read that did not come back as a page of the log. */
const AUDIT_READ_MESSAGE = "The audit log could not be read. Try again in a moment.";

export function AuditSearch() {
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [applied, setApplied] = useState<AppliedFilters>({});
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  // The cursors of the pages walked past, newest last. Its top is the page on
  // screen and an empty stack is the newest page, so `Newer` is exactly "the
  // stack is not empty" and popping cannot invent a cursor.
  const [stack, setStack] = useState<number[]>([]);
  const [rangeError, setRangeError] = useState<string | null>(null);

  const beforeSeq = stack.length === 0 ? null : (stack[stack.length - 1] ?? null);

  const search = useAuditSearch(applied, pageSize, beforeSeq);

  function commit(): void {
    // The pre-check fires NO read at all, which is the half a sentence alone
    // would not buy (§5's fifth risk).
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
    // The same clearing as a filter commit: a cursor read under one page size
    // points into a window the other never asked for.
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
 * Hands the rows on screen to the browser as a file.
 *
 * A pure function of state this component already holds (decision A): no second
 * read, no second cursor walk and no server route, so the file is exactly the
 * page the auditor is looking at and the filename says which slice of the log
 * that is. Paging and then exporting therefore exports the page NOW on screen,
 * which `tests/audit-export.test.tsx::exports the SECOND page's rows after
 * paging to it` pins.
 *
 * The object URL is revoked immediately after the click: an unrevoked one holds
 * the whole file in memory for the life of the document, and a 200-row export
 * chosen repeatedly is the page's memory growing with no cap. The anchor is
 * detached and carries `download`, without which a browser NAVIGATES to the
 * blob and shows the CSV as a page instead of saving it. All three are pinned
 * by `::clicks a named anchor at that url and revokes it afterwards`, against
 * the stubs decision C names.
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

/** The sentence for whatever went wrong, one alert either way. */
function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return error.code === "VALIDATION" ? AUDIT_VALIDATION_MESSAGE : failureMessage(error.code);
  }
  return AUDIT_READ_MESSAGE;
}
