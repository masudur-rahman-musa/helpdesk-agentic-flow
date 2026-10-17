/**
 * How wire timestamps render as text. Pure string slices, never a `Date`, so
 * what a reader sees never depends on the browser's time zone.
 *
 * Pinned by `tests/date-format.test.ts` and
 * `tests/features/admin/killswitch/killswitch-format.test.ts`.
 */

/** How many characters of the wire string carry the date. */
const DATE_PRECISION = 10;

/**
 * A wire timestamp as its date only: `2026-09-13T14:05:09+00:00` renders as
 * `2026-09-13`. A shorter string comes back as is, the same as `slice` does.
 */
export function formatIsoDate(at: string): string {
  return at.slice(0, DATE_PRECISION);
}

/** How many characters of the wire string carry the date and the minute. */
const MINUTE_PRECISION = 16;

/**
 * A switch's `at`, as its date AND its minute in UTC:
 * `2026-09-13T14:05:09+00:00` renders as `2026-09-13 14:05 UTC` (decision B).
 *
 * The minute is carried because on this surface the fact is how long ago
 * somebody stopped a tool — "stopped ten minutes ago" and "stopped four days
 * ago" are different operational situations, and the date prefix the five
 * neighbouring screens use reads identically for both.
 *
 * A slice of the string and never a `Date`: `toLocaleString` answers a
 * different day either side of midnight, and a string carrying no offset
 * (`2026-09-13T14:05:09`) is parsed as LOCAL time by every JS engine, so the
 * hour an operator reads would depend on the browser's zone. The field is
 * already aware — `killswitch/switch.py` writes `datetime.now(UTC)` — so the
 * ` UTC` suffix states what the value is rather than converting it.
 *
 * A string too short to hold a minute is returned UNCHANGED rather than sliced
 * into a half date: `2026-09-13` would otherwise render as `2026-09-13 UTC`, a
 * date wearing a zone it does not carry, and 15 characters as a half minute
 * that reads as a real time while being off by up to nine. Both directions of
 * the boundary are pinned by `tests/features/admin/killswitch/killswitch-format.test.ts::formatSwitchInstant
 * — the strings it formats` (exactly 16 renders) and `::— the strings it returns
 * untouched` (15 does not).
 */
export function formatSwitchInstant(at: string): string {
  if (at.length < MINUTE_PRECISION) {
    return at;
  }
  return `${at.slice(0, MINUTE_PRECISION).replace("T", " ")} UTC`;
}
