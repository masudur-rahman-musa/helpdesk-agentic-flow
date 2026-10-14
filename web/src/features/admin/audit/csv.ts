/**
 * The bytes of `/admin/audit`'s exported file.
 *
 * Everything the export does that a test can pin without a DOM lives here — the
 * header, the cell escaping, the injection guard, the row assembly and the file
 * name — so `tests/audit-csv.test.ts` holds each as a literal and the component
 * owes only the download (§2).
 *
 * The file carries the WIRE values, never the screen's (decision H). `no actor`
 * and `no subject id` are labels written for a table an eye scans: in a
 * spreadsheet filter the first reads as an actor literally named "no actor",
 * and a hash truncated to the 12 characters the table shows could not be
 * checked against any chain verification, which is the one thing this file
 * exists to allow. Pinned by `::writes nulls as empty cells, the full timestamp
 * and hash, and a compact payload`.
 *
 * Nothing here reads a clock, a locale or the DOM: given the same rows it
 * answers the same string, which is what lets §6 assert whole files.
 */

import { type AuditEventRow } from "@/features/admin/audit/schema";

/**
 * The nine wire field names, in the order `search_audit` declares them.
 *
 * Not `EventTable`'s order and not its words: a column headed `Event` holding
 * an `event_type` does not paste back into the `Event type` filter, and two
 * exports compared column by column would be comparing different columns.
 * Pinned as this literal by `tests/audit-csv.test.ts::heads the file with the
 * nine wire field names in the tool's order`.
 */
export const AUDIT_CSV_HEADER =
  "seq,occurred_at,actor_kind,actor_ref,event_type,subject_type,subject_id,payload,hash";

/**
 * The Blob's type. The charset is declared HERE because no BOM is written
 * (decision G): a leading BOM is a byte inside the first header cell for every
 * reader but Excel, and seed data is ASCII by a project rule. Pinned by
 * `::declares the media type as text/csv with the utf-8 charset`.
 */
export const AUDIT_CSV_MIME = "text/csv;charset=utf-8";

/** RFC 4180: a cell containing any of these four is wrapped in double quotes. */
const MUST_QUOTE = ['"', ",", "\r", "\n"];

/**
 * The six characters a spreadsheet reads as the start of a formula.
 *
 * `actor_ref`, `subject_id` and every value inside `payload` carry text a user
 * typed, and a spreadsheet is where an auditor opens this file, so
 * `=HYPERLINK(...)` in an export is the cheapest phishing surface in the
 * product (decision B).
 */
const FORMULA_LEADS = ["=", "+", "-", "@", "\t", "\r"];

/**
 * One cell, guarded then quoted — in that order, and never the other way.
 *
 * The apostrophe goes INSIDE the wrapper: a value quoted first and prefixed
 * after reads as a malformed field to every parser, and the leading character
 * is then outside the cell a spreadsheet evaluates. The guard is on the FIRST
 * character only; a rule matching the character anywhere would prefix every
 * hyphenated event type, every ISO timestamp and every UUID subject id in the
 * file. Both halves are pinned one case per character by
 * `::prefixes a cell beginning with $name` and `::leaves $name alone anywhere
 * but the first character`, and their interaction by `::prefixes and wraps a
 * cell that needs both, in that order`.
 */
function cell(value: string): string {
  const guarded = FORMULA_LEADS.includes(value.slice(0, 1)) ? `'${value}` : value;
  if (!MUST_QUOTE.some((character) => guarded.includes(character))) {
    return guarded;
  }
  return `"${guarded.replaceAll('"', '""')}"`;
}

/**
 * The whole file for the rows given, in the order given.
 *
 * Nothing is re-sorted: the page renders what the tool answered, newest first,
 * and an order imposed here would make the file disagree with the screen it was
 * taken from. Rows are TERMINATED with CRLF rather than joined by it, so a
 * reader appending to the file cannot run two events together; pinned by
 * `::joins rows with CRLF and ends the file with one` and, for no rows at all,
 * by `::writes the header and one CRLF for an empty list`.
 *
 * A null `actor_ref` or `subject_id` becomes an EMPTY cell, and `payload` is
 * compact `JSON.stringify` — the indented form the table renders would put a
 * block of lines inside one quoted cell.
 */
export function toCsv(events: AuditEventRow[]): string {
  const lines = events.map((event) =>
    [
      String(event.seq),
      event.occurred_at,
      event.actor_kind,
      event.actor_ref ?? "",
      event.event_type,
      event.subject_type,
      event.subject_id ?? "",
      JSON.stringify(event.payload),
      event.hash,
    ]
      .map(cell)
      .join(","),
  );
  return [AUDIT_CSV_HEADER, ...lines].map((line) => `${line}\r\n`).join("");
}

/**
 * `audit-{newest seq}-{oldest seq}.csv` for the rows exported (decision F).
 *
 * Deterministic, so no clock reaches the test, and it says which slice of the
 * log the file holds: two exports of two pages cannot collide under one name,
 * and decision A's page-at-a-time limit is visible in the filename rather than
 * inferred. A one-row page carries that row's seq at both ends rather than a
 * second naming shape. Pinned by `::names the file for the seq range of the
 * rows it holds` and `::names a one-row page with that row's seq at both ends`.
 *
 * No caller reaches this with an empty list: `Export CSV` is absent on an empty
 * page (§1), which is why §6 pins no answer for one.
 */
export function csvFilename(events: AuditEventRow[]): string {
  const newest = events[0]?.seq ?? 0;
  const oldest = events[events.length - 1]?.seq ?? newest;
  return `audit-${newest}-${oldest}.csv`;
}
