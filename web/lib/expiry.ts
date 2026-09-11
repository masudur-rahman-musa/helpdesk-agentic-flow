/**
 * The countdown `/access` renders, as arithmetic.
 *
 * A pure function taking its clock as an argument rather than reading the
 * system one, so the row a user sees can be pinned to a fixed instant:
 * `tests/expiry.test.ts`'s five-row table passes `NOW` explicitly and fails
 * every row for an implementation that reaches for `new Date()` itself.
 *
 * Whole days are truncated TOWARDS THE PAST, never rounded. Nine days and
 * twenty-three hours is "9 days left", because a grant that ends tomorrow
 * morning must not read as two days, and a grant that ended two hours ago
 * answers -1 rather than 0, because access already gone must never read as
 * ending today (both pinned by that table).
 *
 * An unreadable timestamp answers `null` and not a number: the row then falls
 * back to the raw value instead of rendering `NaN days left` (an earlier change §5,
 * pinned by `::answers null for the unreadable timestamp`).
 */

const MILLISECONDS_PER_DAY = 86_400_000;

/** Whole days from `now` until `expiresAt`, or `null` if it cannot be read. */
export function daysUntil(expiresAt: string, now: Date): number | null {
  const expiry = Date.parse(expiresAt);
  if (Number.isNaN(expiry)) {
    return null;
  }
  return Math.floor((expiry - now.getTime()) / MILLISECONDS_PER_DAY);
}
