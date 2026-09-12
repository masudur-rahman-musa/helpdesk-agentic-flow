/**
 * The parts of `/admin/audit` a test can pin without a DOM: the
 * four string caps, the page sizes and the two date-to-instant compositions.
 *
 * Nothing here talks to the wire. `components/admin/audit-search.tsx` composes
 * the query string out of these, and 24a's route stays the authority on every
 * one of them — this is the pre-check a project rule asks for on the surface
 * that PRODUCES the string, not a second opinion about what the tool accepts.
 *
 * Pinned by `tests/audit-filters.test.ts`, one exact assertion per value.
 */

/**
 * The four text filters, capped at the length of their own Postgres column:
 * `audit_events.event_type` and `.subject_type` are `String(64)`,
 * `.subject_id` and `.actor_ref` `String(256)` (`models/audit.py`, read by the
 * tool through `read.py::_column_length`).
 *
 * Retyped here because a browser cannot import a SQLAlchemy model, and keyed by
 * the WIRE parameter name rather than by the control's label, so the record
 * reads the same as the query string it caps. A widened column would leave this
 * form refusing input the tool accepts, which is the safe direction and visible:
 * `tests/audit-filters.test.ts` pins all four at their exact value and 24a's
 * route cases pin the same numbers against the tool's own constants, so the pair
 * cannot drift silently the dangerous way.
 */
export const AUDIT_FILTER_MAX_LENGTHS = {
  event_type: 64,
  subject_type: 64,
  subject_id: 256,
  actor_ref: 256,
} as const;

/**
 * The `Page size` choices (decision G). The tool's own default and its maximum
 * with one step between, so every value is inside `SEARCH_LIMIT_MIN`-
 * `SEARCH_LIMIT_MAX` (`1`-`200`) by construction and no new bound is
 * introduced.
 */
export const PAGE_SIZES = [50, 100, 200] as const;

/** What the page opens with, and the `limit` of its first read. */
export const DEFAULT_PAGE_SIZE = 50;

/**
 * The `From` control's day, as the first instant of it.
 *
 * Composed rather than converted (decision C): `new Date(value).toISOString()`
 * answers whatever the running process's zone makes it, so a search for one day
 * would cover a different one either side of a date line. The explicit `Z` is
 * what keeps a naive instant off a `timestamptz` column.
 */
export function fromInstant(value: string): string {
  return `${value}T00:00:00Z`;
}

/**
 * The `To` control's day, as the LAST second of it: the tool filters with `<=`
 * (`audit/search.py::_filters`), so a `To` composed at midnight would exclude
 * every event of the day the auditor asked about.
 */
export function toInstant(value: string): string {
  return `${value}T23:59:59Z`;
}

/**
 * Whether the two controls describe a window with nothing in it.
 *
 * Both ends are inclusive, so the same date at both ends is a legal one-day
 * range and answers false — the commonest search this page runs. An end left
 * empty is no filter at all rather than a boundary, so a raw string compare
 * (`""` sorts before every date) is not what this asks: a filled `From` beside
 * an empty `To` is not inverted. Pinned over all seven rows by
 * `tests/audit-filters.test.ts::isInvertedRange`.
 */
export function isInvertedRange(from: string, to: string): boolean {
  if (from === "" || to === "") {
    return false;
  }
  return from > to;
}
