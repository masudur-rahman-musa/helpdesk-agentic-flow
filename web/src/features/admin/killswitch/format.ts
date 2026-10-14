/**
 * The parts of `/admin/killswitch` a test can pin without a DOM:
 * how a switch's instant renders, and the sentence a failed board read shows.
 *
 * Nothing here talks to the wire. `components/admin/kill-switch-board.tsx`
 * renders through both, the way `audit-search.tsx` renders through
 * `lib/audit-filters.ts` — a pure module with its own test file, so the instant
 * is pinned without rendering a board.
 *
 * Pinned by `tests/killswitch-format.test.ts`, one exact assertion per value.
 */

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
 * the boundary are pinned by `tests/killswitch-format.test.ts::formatSwitchInstant
 * — the strings it formats` (exactly 16 renders) and `::— the strings it returns
 * untouched` (15 does not).
 */
export function formatSwitchInstant(at: string): string {
  if (at.length < MINUTE_PRECISION) {
    return at;
  }
  return `${at.slice(0, MINUTE_PRECISION).replace("T", " ")} UTC`;
}

/**
 * The sentence for a read that did not come back as a board (decision E).
 *
 * Its own string rather than `FAILURE_MESSAGES.default`, which says only that
 * something went wrong and nothing about WHICH thing could not be read — on a
 * page whose alternative reading is "the platform is clean". Pinned verbatim by
 * `tests/killswitch-format.test.ts::KILL_SWITCH_READ_MESSAGE`.
 */
export const KILL_SWITCH_READ_MESSAGE =
  "The kill switches could not be read. Try again in a moment.";
