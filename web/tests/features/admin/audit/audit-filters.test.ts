import { describe, expect, it } from "vitest";

import {
  AUDIT_FILTER_MAX_LENGTHS,
  DEFAULT_PAGE_SIZE,
  fromInstant,
  isInvertedRange,
  PAGE_SIZES,
  toInstant,
} from "@/features/admin/audit/filters";

/**
 * The parts of `/admin/audit` a test can
 * pin without a DOM — the four string caps, the page sizes, the two
 * date-to-instant compositions and the inverted-range predicate. No component,
 * no fetch, no clock.
 *
 * Shapes this file fixes:
 * - `AUDIT_FILTER_MAX_LENGTHS` is keyed by the four WIRE parameter names —
 *   `event_type`, `subject_type`, `subject_id`, `actor_ref` — which is how
 *   the route names them, not by the accessible names of the controls
 *   (`Actor` is the control, `actor_ref` is the parameter).
 * - `PAGE_SIZES` is a readonly tuple of NUMBERS, asserted with `toEqual` in
 *   order, because the select offers them in the order 50, 100, 200 and
 *   opens on the first.
 * - `fromInstant` and `toInstant` take the `type="date"` control's own value
 *   (`YYYY-MM-DD`) and return the composed instant. Neither is given a value
 *   the browser cannot produce: what an empty control does is `isInvertedRange`'s
 *   business and the page's, and an empty control sends no parameter at
 *   all, so there is nothing here for the composers to answer.
 *
 * Every expectation is a literal. Reading the caps off some
 * other module, or recomputing `2026-09-12T00:00:00Z` with `Date`, would pass
 * for whatever the implementation chose.
 */

/**
 * The four caps, each at the length of its own Postgres column
 * (`models/audit.py`: `String(64)` twice, `String(256)` twice), as the page requires.
 */
const CAPS: ReadonlyArray<{ field: keyof typeof AUDIT_FILTER_MAX_LENGTHS; cap: number }> = [
  { field: "event_type", cap: 64 },
  { field: "subject_type", cap: 64 },
  { field: "subject_id", cap: 256 },
  { field: "actor_ref", cap: 256 },
];

describe("AUDIT_FILTER_MAX_LENGTHS", () => {
  it.each(CAPS)("caps $field at $cap exactly", ({ field, cap }) => {
    // One exact assertion per cap. Kills one number standing
    // in for all four — a module exporting 256 everywhere lets an auditor type
    // a 200-character `event_type` that the route then refuses with a
    // 400, which is the whole reason for a pre-check on the control
    // that produces the string. It also kills the off-by-one pair (63, 255):
    // a cap one below the column's makes the form refuse an id the tool
    // accepts, and the shortest way to see that is the exact value.
    expect(AUDIT_FILTER_MAX_LENGTHS[field]).toBe(cap);
  });

  it("carries exactly those four keys and no others", () => {
    // Kills a record that grew a fifth key — a cap for `before_seq` or for a
    // control this page does not have — which would read as a validated field
    // nothing validates, and kills one MISSING a key, where
    // `AUDIT_FILTER_MAX_LENGTHS[field]` is `undefined`, `maxLength` is dropped
    // silently and the parametrised rows above would still pass three of four.
    expect(Object.keys(AUDIT_FILTER_MAX_LENGTHS).sort()).toEqual([
      "actor_ref",
      "event_type",
      "subject_id",
      "subject_type",
    ]);
  });
});

describe("the page sizes", () => {
  it("offers exactly 50, 100 and 200, in that order", () => {
    // The sizes as a value. Kills a list carrying a size
    // outside the tool's own `SEARCH_LIMIT_MIN`-`SEARCH_LIMIT_MAX` bound of
    // `1`-`200` — a 500 option is refused `VALIDATION` by the route and the
    // auditor sees a refusal for a control the page offered them — and kills a
    // reordered list, because the page opens on the FIRST option and
    // the select's own case reads its labels in order.
    expect(PAGE_SIZES).toEqual([50, 100, 200]);
  });

  it("opens on 50", () => {
    // 50 is what the page opens with, and the number the first paint's
    // `limit=50` is asserted against. Kills a default of 200: every first paint
    // would then ask the tool for four times the rows, against a `SCAN_CAP` of
    // 500 per call, for a screen nobody has filtered yet. Pinned as a literal
    // rather than as `PAGE_SIZES[0]`, which would be green for any reordering.
    expect(DEFAULT_PAGE_SIZE).toBe(50);
  });

  it("has its default among its options", () => {
    // Kills a default that no option carries: the `Page size` select would
    // render with nothing selected (or with the browser's own first choice),
    // so what the page opens with and what it says it opened with disagree.
    // Neither case above can see that — one reads the list, the other the
    // default.
    expect(PAGE_SIZES).toContain(DEFAULT_PAGE_SIZE);
  });
});

describe("the two date compositions", () => {
  it("composes a From date as midnight UTC", () => {
    // The exact string. Kills a composer
    // that hands the column a NAIVE instant (`2026-09-12T00:00:00`), whose
    // meaning then depends on the server's zone, and kills one going through
    // `new Date(value).toISOString()`, which in jsdom answers whatever the test
    // process's `TZ` makes it and would move the boundary by hours in half the
    // world. The literal is the only form that pins both.
    expect(fromInstant("2026-09-12")).toBe("2026-09-12T00:00:00Z");
  });

  it("composes a To date as the last second of that day", () => {
    // The inclusive half, matching the tool's `<=` (`audit/search.py::_filters`).
    // Kills the obvious wrong pair — a `To` composed as `T00:00:00Z`, which
    // silently excludes every event of the day the auditor asked for, the day
    // most searches are about. Nothing else here would notice: a whole-day
    // range would still return rows, just not today's.
    expect(toInstant("2026-09-12")).toBe("2026-09-12T23:59:59Z");
  });

  it("composes the two ends differently for one date", () => {
    // The pair as a pair. Kills an implementation where `toInstant` is an alias
    // of `fromInstant` (or both read one shared helper), which would make a
    // one-day range an empty window — `occurred_from == occurred_to` matches
    // only an event at exactly midnight — while each case above could still be
    // green for whichever of the two was written first.
    expect(toInstant("2026-09-12")).not.toBe(fromInstant("2026-09-12"));
  });
});

const RANGES = [
  {
    name: "the From date one day after the To date",
    from: "2026-09-12",
    to: "2026-09-11",
    inverted: true,
  },
  {
    name: "the From date a year after the To date",
    from: "2026-09-12",
    to: "2025-09-12",
    inverted: true,
  },
  {
    name: "the same date at both ends, which is a legal one-day range",
    from: "2026-09-12",
    to: "2026-09-12",
    inverted: false,
  },
  {
    name: "the From date before the To date",
    from: "2026-09-11",
    to: "2026-09-12",
    inverted: false,
  },
  { name: "an empty From with a To", from: "", to: "2026-09-12", inverted: false },
  { name: "a From with an empty To", from: "2026-09-12", to: "", inverted: false },
  { name: "both ends empty, which is the first paint", from: "", to: "", inverted: false },
] as const;

describe("isInvertedRange", () => {
  it.each(RANGES)("answers $inverted for $name", ({ from, to, inverted }) => {
    // The predicate the `AUDIT_RANGE_MESSAGE` alert fires from, with its two
    // boundary rows carrying the weight. The same-date row kills a `>=`
    // comparison: an auditor searching one day would be told their From is
    // after their To and no read would ever go out, which is a page that cannot
    // answer its commonest question. The two half-empty rows kill a predicate
    // comparing the raw strings without checking for a value first — `""` sorts
    // before every date, so `("2026-09-12", "")` reads as inverted and filling
    // in only a From would block the search — and the both-empty row is the
    // first paint itself, which must not be refused before it happens. The two
    // true rows kill a predicate that is constantly false, which would let
    // `InvalidTimeRangeError` come back from the tool as a refusal sentence
    // instead.
    expect(isInvertedRange(from, to)).toBe(inverted);
  });
});
