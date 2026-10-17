import { describe, expect, it } from "vitest";

import { daysUntil } from "@/features/access/expiry";

/**
 * The countdown arithmetic `/access` renders,
 * as a pure function. No clock, no component, no fetch.
 *
 * Shapes this file fixes:
 * - the signature is `daysUntil(expiresAt: string, now: Date): number | null`
 *   — the wire value as a STRING (that is what `myGrantSchema` parses to) and
 *   the clock as an argument, answering `null` for anything
 *   `Date` cannot read.
 * - "whole days" means TRUNCATED TOWARDS THE PAST (`Math.floor` of the
 *   difference in days), not rounded. Nine days and twenty-three hours is
 *   still "9 days left": a grant that ends tomorrow morning must never read as
 *   two days, and one that ended two hours ago must never read as zero.
 * - `null` is the answer for an unreadable timestamp, so the row can render
 *   the raw value instead of `NaN days`.
 *
 * Every row passes `NOW` explicitly, so an implementation reading the system
 * clock instead of its own argument fails the whole table on any day but
 * 2026-06-01.
 */

/** A fixed instant, so every expectation below is arithmetic and not today. */
const NOW = new Date("2026-06-01T12:00:00Z");

const ROWS = [
  {
    name: "exactly nine days ahead",
    expiresAt: "2026-06-10T12:00:00Z",
    days: 9,
  },
  {
    name: "nine days and twenty-three hours ahead, truncated not rounded",
    expiresAt: "2026-06-11T11:00:00Z",
    days: 9,
  },
  {
    name: "later on the day it expires",
    expiresAt: "2026-06-01T23:59:00Z",
    days: 0,
  },
  {
    name: "two hours past, which is already expired and not today",
    expiresAt: "2026-06-01T10:00:00Z",
    days: -1,
  },
  {
    name: "exactly one day past",
    expiresAt: "2026-05-31T12:00:00Z",
    days: -1,
  },
] as const;

const UNREADABLE = ["not a date", "", "2026-13-45T00:00:00Z", "30 days"] as const;

describe("daysUntil", () => {
  it.each(ROWS)("answers $days for a grant $name", ({ expiresAt, days }) => {
    // The whole table catches an implementation that reads the system clock
    // instead of its `now` argument (a component cannot be tested
    // against a clock it does not own). Row by row: the nine-day pair catches
    // `Math.round`, which turns twenty-three hours into a tenth day the grant
    // does not have; the same-day row catches an implementation counting
    // CALENDAR boundaries with a `+1`, which would read "1 day left" for a
    // grant ending tonight; and the two-hour row catches `Math.trunc` or
    // `Math.ceil`, both of which answer `0` for an ALREADY EXPIRED grant and
    // so would render "0 days left" over access the user no longer holds.
    expect(daysUntil(expiresAt, NOW)).toBe(days);
  });

  it.each(UNREADABLE)("answers null for the unreadable timestamp %j", (expiresAt) => {
    // Catches the bare `Math.floor((Date.parse(x) - now) / DAY)`: `Date.parse`
    // answers `NaN` for every row here, the arithmetic propagates it, and the
    // grant renders as "NaN days left". `null` is what lets the row fall
    // back to the raw value, so answering `0` here would be worse than the NaN
    // — it would claim the grant expires today.
    expect(daysUntil(expiresAt, NOW)).toBeNull();
  });
});
