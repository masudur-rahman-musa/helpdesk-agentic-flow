import { describe, expect, it } from "vitest";

import {
  DIGEST_PREFIX,
  formatDigest,
  LIVE_VERSION_TEXT,
  NO_CHUNKS_TEXT,
  NO_LIVE_VERSION_TEXT,
  NOT_LIVE_VERSION_TEXT,
  POLICIES_EMPTY_TEXT,
  POLICIES_READ_MESSAGE,
} from "@/features/admin/policies/format";

/**
 * `policies-format.test.ts`: the parts of `/admin/policies` a test
 * can pin without a DOM — how a version's digest renders, and the six strings
 * the board shows. No component, no fetch, no clock, no backing service. (The neighbours are
 * `policies-schema.test.ts` and `policies-proxy-routes.test.ts`, same folder.)
 *
 * The unit under test is `web/src/features/admin/policies/format.ts`, which exports
 * `formatDigest(sha256: string): string`, `DIGEST_PREFIX` and the six
 * sentences — the shape `lib/date-format.ts` already carries. The instant
 * is NOT here: `formatSwitchInstant` is imported from `lib/date-format.ts`
 * unchanged and stays pinned by `tests/killswitch-format.test.ts`,
 * so no case in this file formats a date.
 *
 * Shapes this file fixes:
 * - `formatDigest` takes the whole `sha256` string and answers the TEXT of the
 *   digest cell only. The `title` attribute carrying the full 64 characters is
 *   the component's, asserted in `policies-board.test.tsx`, because
 *   a pure function cannot hold an attribute.
 * - `DIGEST_PREFIX` is a `number` (12), not a string, so the component slices
 *   with it rather than re-typing the 12.
 * - the board shows SIX sentence constants, not five
 *   (`POLICIES_READ_MESSAGE`, `POLICIES_EMPTY_TEXT`, `LIVE_VERSION_TEXT`,
 *   `NOT_LIVE_VERSION_TEXT`, `NO_LIVE_VERSION_TEXT`, `NO_CHUNKS_TEXT`). Six is what the board
 * needs — `Live` and
 *   `Not live` are two of them — so six is what this file pins.
 *
 * Every expectation is a literal. Recomputing an expected
 * prefix with `slice(0, DIGEST_PREFIX)` here would pass for whatever the module
 * chose, including the wrong length.
 */

/** A fake 64-character digest whose first 12 characters differ from its rest. */
const LONG_DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

describe("formatDigest — the digests it shortens", () => {
  const SHORTENED: ReadonlyArray<{ name: string; sha256: string; rendered: string }> = [
    { name: "a full 64-character digest", sha256: LONG_DIGEST, rendered: "0123456789ab" },
    {
      name: "a digest of exactly 13 characters",
      sha256: "0123456789abX",
      rendered: "0123456789ab",
    },
  ];

  it.each(SHORTENED)("renders $name as its first twelve characters", ({ sha256, rendered }) => {
    // As exact strings.
    //
    // The first row kills a module that returns the whole `sha256`: 64 monospace
    // characters cost the other five columns their width on every version row,
    // which is the entire reason a prefix was chosen. It equally kills an
    // off-by-one in either direction — `slice(0, 11)` and `slice(0, 13)` both
    // answer a string this literal refuses — and kills a module that shortens
    // from the WRONG END (`slice(-12)` answers "6789abcdef"), where two
    // versions of one document would be told apart by characters an ingest log
    // prints at the other end of the value.
    //
    // The second row is the exclusive side of the inclusive boundary below: one
    // character longer than the prefix, so it is the shortest input a correct
    // module still cuts. It kills a guard written `length <= DIGEST_PREFIX + 1`,
    // which returns "0123456789abX" whole — a digest cell one character wider
    // than every other row's, read as if the value itself were odd.
    expect(formatDigest(sha256)).toBe(rendered);
  });
});

describe("formatDigest — the digests it returns whole", () => {
  const WHOLE: ReadonlyArray<{ name: string; sha256: string }> = [
    { name: "a digest of exactly twelve characters", sha256: "0123456789ab" },
    { name: "a digest of eleven characters", sha256: "0123456789a" },
    { name: "the empty string", sha256: "" },
  ];

  it.each(WHOLE)("returns $name unchanged", ({ sha256 }) => {
    // The inclusive boundary from the other direction: 12 is
    // the last length that is returned whole, 13 (above) the first that is cut.
    //
    // The first row kills the off-by-one `slice(0, DIGEST_PREFIX - 1)` and any guard
    // written `length < DIGEST_PREFIX ? sha256 : slice(...)` combined with a
    // prefix of 11 — both answer eleven characters for a value that is already
    // exactly the display width.
    //
    // The second and third rows kill a module that marks the cut UNCONDITIONALLY — the
    // `${sha256.slice(0, 12)}…` shape, which is how a truncated value is
    // usually written — because it would append the marker to a digest it did
    // not truncate, telling an admin comparing against an ingest log that there
    // are characters the cell is hiding when there are none. A bare
    // `slice(0, 12)` with no guard at all is GREEN for these three rows, since
    // `slice` cannot lengthen a short string; that is the requirements' "returned
    // UNCHANGED rather than sliced" being weaker than it reads, and the first row above
    // is where the prefix length itself is killed.
    expect(formatDigest(sha256)).toBe(sha256);
  });
});

describe("policies-format — the values the board reads", () => {
  it("fixes DIGEST_PREFIX at twelve", () => {
    // The exact literal for the one number this module adds, asserted as a
    // number so the component can slice with it. Every other case in this file
    // and in `policies-board.test.tsx` asserts the rendered STRINGS, which stay
    // green only while the prefix is 12; this literal is what makes the
    // constant itself wrong to change. Kills a module exporting the 12 as
    // `"12"`, where `sha256.slice(0, DIGEST_PREFIX)` still type-errors nowhere
    // useful in the component and every cell renders empty.
    expect(DIGEST_PREFIX).toBe(12);
  });

  const SENTENCES: ReadonlyArray<{ name: string; value: string; sentence: string }> = [
    {
      name: "POLICIES_READ_MESSAGE",
      value: POLICIES_READ_MESSAGE,
      sentence: "The policy documents could not be read. Try again in a moment.",
    },
    {
      name: "POLICIES_EMPTY_TEXT",
      value: POLICIES_EMPTY_TEXT,
      sentence: "No policy documents have been ingested.",
    },
    { name: "LIVE_VERSION_TEXT", value: LIVE_VERSION_TEXT, sentence: "Live" },
    { name: "NOT_LIVE_VERSION_TEXT", value: NOT_LIVE_VERSION_TEXT, sentence: "Not live" },
    { name: "NO_LIVE_VERSION_TEXT", value: NO_LIVE_VERSION_TEXT, sentence: "No live version" },
    { name: "NO_CHUNKS_TEXT", value: NO_CHUNKS_TEXT, sentence: "No chunks ingested" },
  ];

  it.each(SENTENCES)("$name is exactly the sentence the board shows", ({ value, sentence }) => {
    // The exact literals for all six strings. Every case in
    // `policies-board.test.tsx` READS these constants, so that file stays green
    // for whatever wording the module chooses; only these literals fix them.
    //
    // Kills a `POLICIES_READ_MESSAGE` filled with `FAILURE_MESSAGES.default`
    // ("Something went wrong. Nothing was changed."), which says nothing about
    // WHICH thing could not be read on a page whose alternative reading is "this
    // tenant has ingested no policy at all".
    //
    // Kills a `NOT_LIVE_VERSION_TEXT` of `""` — "never a blank
    // cell" — and a `NOT_LIVE_VERSION_TEXT` of `Live` or a `LIVE_VERSION_TEXT`
    // of `Not live`, the swap that would report a superseded version as the one
    // the retriever is answering from. A containment check could not: "Live" is
    // a substring of "Not live", so only `toBe` separates the two.
    //
    // Kills a `NO_LIVE_VERSION_TEXT` reworded to something the empty state
    // already says: the half-landed ingest is the state this page exists to
    // show, and "no live version" of one document is not "no documents".
    expect(value).toBe(sentence);
  });
});
