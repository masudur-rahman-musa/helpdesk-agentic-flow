"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { AUDIT_CSV_MIME, csvFilename, toCsv } from "@/features/admin/audit/csv";
import {
  AUDIT_FILTER_MAX_LENGTHS,
  DEFAULT_PAGE_SIZE,
  fromInstant,
  isInvertedRange,
  PAGE_SIZES,
  toInstant,
} from "@/features/admin/audit/filters";
import { type AuditEventRow, auditSearchSchema } from "@/features/admin/audit/schema";
import { failureMessage } from "@/features/chat/errors";
import { WireRefusedError, readWire } from "@/lib/wire-read";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Input, Label } from "@/components/ui/field";
import { PageHeader } from "@/components/ui/page-header";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

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

/** The four text controls, each with the wire parameter it fills and its cap. */
const TEXT_FILTERS = [
  { label: "Event type", param: "event_type" },
  { label: "Subject type", param: "subject_type" },
  { label: "Subject id", param: "subject_id" },
  { label: "Actor", param: "actor_ref" },
] as const;

/** The applied filters, as the wire parameters they already are. */
type AppliedFilters = Readonly<Record<string, string>>;

/** What the controls hold right now, keyed the same way plus the two dates. */
type Draft = Record<string, string>;

const EMPTY_DRAFT: Draft = {
  event_type: "",
  subject_type: "",
  subject_id: "",
  actor_ref: "",
  from: "",
  to: "",
};

/** One page of the log, as this component reads it. */
type SearchPage = { events: AuditEventRow[]; nextBeforeSeq: number | null };

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

  const search = useQuery({
    queryKey: ["admin", "audit", "search", applied, pageSize, beforeSeq],
    queryFn: () => fetchSearch(applied, pageSize, beforeSeq),
  });

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

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          commit();
        }}
      >
        {TEXT_FILTERS.map((filter) => (
          <div key={filter.param} className="flex w-56 flex-col gap-1">
            <Label htmlFor={`audit-${filter.param}`}>{filter.label}</Label>
            <Input
              id={`audit-${filter.param}`}
              type="text"
              value={draft[filter.param] ?? ""}
              maxLength={AUDIT_FILTER_MAX_LENGTHS[filter.param]}
              onChange={(event) =>
                setDraft((current) => ({ ...current, [filter.param]: event.target.value }))
              }
            />
          </div>
        ))}

        {(
          [
            { key: "from", label: "From" },
            { key: "to", label: "To" },
          ] as const
        ).map((end) => (
          <div key={end.key} className="flex w-44 flex-col gap-1">
            <Label htmlFor={`audit-${end.key}`}>{end.label}</Label>
            <Input
              id={`audit-${end.key}`}
              type="date"
              value={draft[end.key] ?? ""}
              onChange={(event) =>
                setDraft((current) => ({ ...current, [end.key]: event.target.value }))
              }
            />
          </div>
        ))}

        <div className="flex w-32 flex-col gap-1">
          <Label htmlFor="audit-limit">Page size</Label>
          <select
            id="audit-limit"
            value={pageSize}
            onChange={(event) => choosePageSize(Number(event.target.value))}
            className="rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </div>

        <Button type="submit">Search</Button>
      </form>

      {rangeError !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {rangeError}
        </p>
      ) : null}

      {refusal !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {refusal}
        </p>
      ) : null}

      {search.isSuccess && events.length === 0 ? (
        <EmptyState title="No events match that search." />
      ) : null}

      {search.isSuccess && events.length > 0 ? <EventTable events={events} /> : null}

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
 * The page of events, newest first, in the order the tool answered.
 *
 * Nothing is re-sorted: 13b returns newest first and an ordering imposed here
 * would be a second opinion about the one thing the log exists to show. The
 * timestamp renders as its 19-character ISO prefix, never
 * `toLocaleDateString` (which answers a different day either side of a date
 * line) and never the raw value, whose six digits of microseconds cost a column
 * an auditor scans.
 */
function EventTable({ events }: { events: AuditEventRow[] }) {
  return (
    <Table aria-label="Audit events">
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Seq</TableHead>
          <TableHead scope="col">Occurred</TableHead>
          <TableHead scope="col">Event</TableHead>
          <TableHead scope="col">Actor kind</TableHead>
          <TableHead scope="col">Actor</TableHead>
          <TableHead scope="col">Subject</TableHead>
          <TableHead scope="col">Subject id</TableHead>
          <TableHead scope="col">Hash</TableHead>
          <TableHead scope="col">Payload</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {events.map((event) => (
          <TableRow key={event.seq} className="align-top">
            <TableCell>{event.seq}</TableCell>
            <TableCell>{event.occurred_at.slice(0, 19)}</TableCell>
            {/* The raw `event_type` (decision B): the vocabulary is open, so a
                hand-kept map would be stale the day a row adds a type, and the
                literal is what an auditor pastes back into the filter. */}
            <TableCell>{event.event_type}</TableCell>
            <TableCell>{event.actor_kind}</TableCell>
            <TableCell>{event.actor_ref ?? "no actor"}</TableCell>
            <TableCell>{event.subject_type}</TableCell>
            <TableCell>{event.subject_id ?? "no subject id"}</TableCell>
            <TableCell title={event.hash}>{event.hash.slice(0, 12)}</TableCell>
            <TableCell>
              {/* `payload` is an arbitrary object by design (13b decision E),
                  so it is stringified whole rather than read key by key: a row
                  reading named keys renders blanks for every event type but
                  the one it was written for. Collapsed, because a dozen lines
                  of JSON between every two rows is not a table anyone scans. */}
              <details aria-label={`Payload for event ${event.seq}`}>
                <summary>Payload</summary>
                <pre className="whitespace-pre-wrap text-xs">
                  {JSON.stringify(event.payload, null, 2)}
                </pre>
              </details>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
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

/**
 * The applied filters, as wire parameters, out of what the controls hold.
 *
 * A control left empty contributes NOTHING. The two dates are composed into
 * instants here rather than sent raw, so a `timestamptz` column never meets a
 * value whose meaning depends on the server's zone (decision C).
 */
function filtersOf(draft: Draft): AppliedFilters {
  const filters: Record<string, string> = {};
  for (const filter of TEXT_FILTERS) {
    const value = draft[filter.param] ?? "";
    if (value !== "") {
      filters[filter.param] = value;
    }
  }
  if ((draft.from ?? "") !== "") {
    filters.occurred_from = fromInstant(draft.from ?? "");
  }
  if ((draft.to ?? "") !== "") {
    filters.occurred_to = toInstant(draft.to ?? "");
  }
  return filters;
}

/**
 * One page of the log for the applied filters, the chosen size and the cursor.
 *
 * `before_seq` belongs to paging alone and is never a form field: it is the
 * `next_before_seq` the tool itself handed back, so the page is never asked for
 * a boundary this code invented.
 */
async function fetchSearch(
  filters: AppliedFilters,
  pageSize: number,
  beforeSeq: number | null,
): Promise<SearchPage> {
  const params = new URLSearchParams({ limit: String(pageSize) });
  for (const [name, value] of Object.entries(filters)) {
    params.set(name, value);
  }
  if (beforeSeq !== null) {
    params.set("before_seq", String(beforeSeq));
  }

  const payload = await readWire(`/api/admin/audit/search?${params.toString()}`, auditSearchSchema);
  if (payload === null) {
    // The redirect answers what this read answers when it finds nothing, so no
    // third state appears on screen while the browser navigates to `/login`
    //.
    return { events: [], nextBeforeSeq: null };
  }
  return { events: payload.events, nextBeforeSeq: payload.next_before_seq };
}
