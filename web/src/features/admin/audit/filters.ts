/** Audit search limits and date helpers. The server still checks every value. */

/**
 * Each cap matches its database column length. A browser cannot import the
 * model, so the numbers are copied here.
 */
export const AUDIT_FILTER_MAX_LENGTHS = {
  event_type: 64,
  subject_type: 64,
  subject_id: 256,
  actor_ref: 256,
} as const;

/** All inside the server's allowed range of 1 to 200. */
export const PAGE_SIZES = [50, 100, 200] as const;

export const DEFAULT_PAGE_SIZE = 50;

/** Built as a string, not via `Date`, so the server's time zone never shifts the day. */
export function fromInstant(value: string): string {
  return `${value}T00:00:00Z`;
}

/** The last second of the day: midnight would drop that whole day. */
export function toInstant(value: string): string {
  return `${value}T23:59:59Z`;
}

/** Both ends are inclusive, and an empty end is no filter, not a bound. */
export function isInvertedRange(from: string, to: string): boolean {
  if (from === "" || to === "") {
    return false;
  }
  return from > to;
}
