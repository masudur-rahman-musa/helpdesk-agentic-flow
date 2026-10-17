/**
 * The audit CSV export. It writes raw values, not the screen's labels: the
 * full hash and empty cells, so the file can be checked and filtered.
 */

import { type AuditEventRow } from "@/features/admin/audit/schema";

/** Server field names, so a column pastes straight back into the filter. */
export const AUDIT_CSV_HEADER =
  "seq,occurred_at,actor_kind,actor_ref,event_type,subject_type,subject_id,payload,hash";

/** The charset is declared here because no BOM is written. */
export const AUDIT_CSV_MIME = "text/csv;charset=utf-8";

/** RFC 4180: a cell holding any of these is quoted. */
const MUST_QUOTE = ['"', ",", "\r", "\n"];

/**
 * Characters a spreadsheet reads as a formula start. Cells carry user text,
 * so `=HYPERLINK(...)` would be a phishing link in the auditor's spreadsheet.
 */
const FORMULA_LEADS = ["=", "+", "-", "@", "\t", "\r"];

/**
 * Guard, then quote, so the apostrophe sits inside the quotes. Only the first
 * character counts, or every timestamp and UUID would get a prefix.
 */
function cell(value: string): string {
  const guarded = FORMULA_LEADS.includes(value.slice(0, 1)) ? `'${value}` : value;
  if (!MUST_QUOTE.some((character) => guarded.includes(character))) {
    return guarded;
  }
  return `"${guarded.replaceAll('"', '""')}"`;
}

/** Each row ends in CRLF, so appending never runs two rows together. */
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

/** `audit-{newest}-{oldest}.csv`: two pages never share a name. */
export function csvFilename(events: AuditEventRow[]): string {
  const newest = events[0]?.seq ?? 0;
  const oldest = events[events.length - 1]?.seq ?? newest;
  return `audit-${newest}-${oldest}.csv`;
}
