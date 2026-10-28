import { describe, expect, it } from "vitest";

import { KILL_SWITCH_READ_MESSAGE } from "@/features/admin/killswitch/format";
import { formatSwitchInstant } from "@/lib/date-format";

/**
 * `killswitch-format.test.ts`: the parts of `/admin/killswitch` a
 * test can pin without a DOM — how a switch's instant renders, and the sentence
 * a failed read shows. No component, no fetch, no clock, no backing service.
 * (The neighbours are
 * `killswitch-schema.test.ts` and `killswitch-proxy-routes.test.ts`.)
 *
 * The units under test are `formatSwitchInstant` in `web/src/lib/date-format.ts`
 * and `KILL_SWITCH_READ_MESSAGE` in `web/src/features/admin/killswitch/format.ts`; they export
 * `formatSwitchInstant(at: string): string` and `KILL_SWITCH_READ_MESSAGE` —
 * the way `lib/audit-filters.ts` carries the pure helpers.
 *
 * This is the rule these rows encode: the wire string's first 16
 * characters, with `T` replaced by a space, followed by ` UTC`. Never a `Date`
 * and never `toLocaleString`, for the five neighbours' own reason — a formatter
 * that parses the string answers a different day either side of midnight, and a
 * string with no offset (`2026-09-13T14:05:09`) is parsed as LOCAL time by
 * every JS engine, so the hour an operator reads would depend on the browser's
 * zone.
 *
 * Shapes this file fixes:
 * - 16 characters is the INCLUSIVE boundary of "formatted": a string
 *   SHORTER than 16 is returned unchanged, so `"2026-09-13T14:05"` (exactly 16)
 *   is formatted and `"2026-09-13T14:0"` (15) is not. Both rows are here; the
 *   spec states only the short case, and either direction of an off-by-one
 *   satisfies its prose.
 * - "unchanged" is the INPUT string identically, asserted with `toBe`, not a
 *   trimmed or re-padded variant.
 *
 * Every expectation is a literal. Recomputing the expected
 * string with `slice` and `replace` here would pass for whatever the
 * implementation chose, including the wrong one.
 */

const FORMATTED: ReadonlyArray<{ name: string; at: string; rendered: string }> = [
  {
    name: "an aware wire instant",
    at: "2026-09-13T14:05:09+00:00",
    rendered: "2026-09-13 14:05 UTC",
  },
  {
    name: "the same instant with no offset",
    at: "2026-09-13T14:05:09",
    rendered: "2026-09-13 14:05 UTC",
  },
  {
    name: "a string of exactly 16 characters",
    at: "2026-09-13T14:05",
    rendered: "2026-09-13 14:05 UTC",
  },
];

const UNCHANGED: ReadonlyArray<{ name: string; at: string }> = [
  { name: "a string of 15 characters", at: "2026-09-13T14:0" },
  { name: "a bare date", at: "2026-09-13" },
  { name: "the empty string", at: "" },
];

describe("formatSwitchInstant — the strings it formats", () => {
  it.each(FORMATTED)("renders $name as its date and minute in UTC", ({ at, rendered }) => {
    // The three exact strings.
    //
    // The first row kills a formatter built on `Date`: `toLocaleString` renders
    // "9/13/2026, 2:05:09 PM" (and a different DAY either side of midnight in a
    // non-UTC browser), and `toISOString()` renders the `T` and the seconds
    // back. It also kills the neighbours' date-only prefix — "stopped ten
    // minutes ago" and "stopped four days ago" are different operational
    // situations that "2026-09-13" cannot tell apart, which is the whole
    // reason this surface gets a minute.
    //
    // The second row kills a formatter that reaches the answer by STRIPPING the
    // `+00:00` suffix instead of slicing 16 characters: on this input there is
    // no suffix to strip, so it renders "2026-09-13 14:05:09 UTC" — the
    // seconds still on — while the first row stays green for it.
    //
    // The third row is the inclusive boundary: it kills the off-by-one guard
    // `at.length <= 16 ? at : ...`, which returns this minute-precision instant
    // raw, complete with its `T` and with no zone on a page whose entire
    // subject is when somebody stopped a tool.
    expect(formatSwitchInstant(at)).toBe(rendered);
  });
});

describe("formatSwitchInstant — the strings it returns untouched", () => {
  it.each(UNCHANGED)("returns $name unchanged", ({ at }) => {
    // From three directions, each killing a
    // formatter that slices and appends unconditionally.
    //
    // 15 characters would render "2026-09-13 14:0 UTC" — a half minute, which
    // reads as a real time and is off by up to nine minutes.
    //
    // A bare date would render "2026-09-13 UTC", a date wearing a zone it does
    // not carry, so an operator reads a stop as having a time nobody recorded.
    //
    // The empty string would render " UTC": a switch with no instant at all,
    // printed as if the zone were the fact. All three are red for the
    // unconditional shape and green only for one that measures the string
    // first.
    expect(formatSwitchInstant(at)).toBe(at);
  });
});

describe("KILL_SWITCH_READ_MESSAGE", () => {
  it("is exactly the sentence a failed board read shows", () => {
    // The exact literal for the one string this module introduces. Every case in
    // `killswitch-board.test.tsx` READS this constant, so it stays green for
    // whatever wording the module chooses; only this literal fixes it. Kills an
    // implementation that exports the symbol and fills it with
    // `FAILURE_MESSAGES.default` ("Something went wrong. Nothing was changed."),
    // which tells an operator nothing about WHICH thing could not be read, and
    // kills one that reports the failure as an empty board.
    expect(KILL_SWITCH_READ_MESSAGE).toBe(
      "The kill switches could not be read. Try again in a moment.",
    );
  });
});
