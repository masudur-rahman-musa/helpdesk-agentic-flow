/**
 * How server timestamps show as text. String slices, never a `Date`, so what a
 * reader sees does not depend on the browser's time zone.
 */

const DATE_PRECISION = 10;

/** A timestamp as its date only: `2026-09-13T14:05:09+00:00` gives `2026-09-13`. */
export function formatIsoDate(at: string): string {
  return at.slice(0, DATE_PRECISION);
}

const MINUTE_PRECISION = 16;

/**
 * A kill switch time as date and minute in UTC, e.g. `2026-09-13 14:05 UTC`.
 * The minute matters: "stopped ten minutes ago" and "four days ago" are very
 * different. The server stores UTC, so the suffix states it rather than
 * converting. A string too short for a minute comes back unchanged instead of
 * as a half time that looks real.
 */
export function formatSwitchInstant(at: string): string {
  if (at.length < MINUTE_PRECISION) {
    return at;
  }
  return `${at.slice(0, MINUTE_PRECISION).replace("T", " ")} UTC`;
}
