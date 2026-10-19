import { describe, expect, it } from "vitest";

import type { Citation } from "@/features/chat/components/types";
import { tidySources } from "@/features/chat/sources";

/**
 * `sources.test.ts`: `tidySources` merges passages of one document, version
 * and section into one line and drops weak lines. Plain objects only.
 */

function passage(over: Partial<Citation> & { chunkId: string }): Citation {
  return {
    document: "fake-access-policy",
    title: "Fake Access Policy",
    version: "v3",
    headingPath: ["8. Requesting access"],
    text: `text of ${over.chunkId}`,
    score: 0.03,
    ...over,
  };
}

describe("tidySources", () => {
  it("test_same_document_and_section_merge_into_one", () => {
    // Kills an implementation that leaves repeated passages as separate lines,
    // or joins their texts in another order or with another separator.
    const out = tidySources([
      passage({ chunkId: "a", text: "one" }),
      passage({ chunkId: "b", text: "two" }),
      passage({ chunkId: "c", text: "three" }),
    ]);

    expect(out).toHaveLength(1);
    expect(out[0]?.text).toBe("one\n\ntwo\n\nthree");
  });

  it("test_a_different_section_stays_its_own_line", () => {
    // Kills an implementation that merges on document and version only.
    const out = tidySources([
      passage({ chunkId: "a", headingPath: ["8. Requesting access"] }),
      passage({ chunkId: "b", headingPath: ["9. Removing access"] }),
    ]);

    expect(out).toHaveLength(2);
  });

  it("test_a_different_version_stays_its_own_line", () => {
    // Kills an implementation that merges on document and section only.
    const out = tidySources([
      passage({ chunkId: "a", version: "v2" }),
      passage({ chunkId: "b", version: "v3" }),
    ]);

    expect(out).toHaveLength(2);
  });

  it("test_a_different_document_stays_its_own_line", () => {
    // Kills an implementation that merges on version and section only.
    const out = tidySources([
      passage({ chunkId: "a", document: "fake-access-policy" }),
      passage({ chunkId: "b", document: "fake-travel-policy" }),
    ]);

    expect(out).toHaveLength(2);
  });

  it("test_line_keeps_first_place_and_first_key", () => {
    // Kills an implementation that moves a merged line to its last passage's
    // place or takes the last passage's chunkId as its key.
    const out = tidySources([
      passage({ chunkId: "a1", text: "A1" }),
      passage({ chunkId: "b", headingPath: ["9. Removing access"], text: "B" }),
      passage({ chunkId: "a2", text: "A2" }),
    ]);

    expect(out.map((c) => c.chunkId)).toEqual(["a1", "b"]);
    expect(out[0]?.text).toBe("A1\n\nA2");
  });

  it("test_weak_line_is_dropped", () => {
    // Kills an implementation that never drops weak lines.
    const out = tidySources([
      passage({ chunkId: "best", score: 0.0328 }),
      passage({ chunkId: "weak", headingPath: ["9. Removing access"], score: 0.016 }),
    ]);

    expect(out.map((c) => c.chunkId)).toEqual(["best"]);
  });

  it("test_exactly_half_stays", () => {
    // Kills an implementation that drops with <= instead of <, so a line at
    // exactly half the best is lost.
    const out = tidySources([
      passage({ chunkId: "best", score: 0.032 }),
      passage({ chunkId: "half", headingPath: ["9. Removing access"], score: 0.016 }),
    ]);

    expect(out.map((c) => c.chunkId)).toEqual(["best", "half"]);
  });

  it("test_just_under_half_is_dropped", () => {
    // Kills an implementation whose cut is lower than half (for example a third).
    const out = tidySources([
      passage({ chunkId: "best", score: 0.032 }),
      passage({ chunkId: "low", headingPath: ["9. Removing access"], score: 0.0159 }),
    ]);

    expect(out.map((c) => c.chunkId)).toEqual(["best"]);
  });

  it("test_merged_line_ranks_by_its_best_passage", () => {
    // Kills an implementation that scores a merged line by its first or last
    // passage (0.010 or the average), which would drop it below half of 0.032.
    const out = tidySources([
      passage({ chunkId: "m1", headingPath: ["9. Removing access"], score: 0.01 }),
      passage({ chunkId: "best", score: 0.032 }),
      passage({ chunkId: "m2", headingPath: ["9. Removing access"], score: 0.03 }),
    ]);

    expect(out.map((c) => c.chunkId)).toEqual(["m1", "best"]);
  });

  it("test_merged_line_reports_best_score", () => {
    // Kills an implementation that keeps the first passage's score on a merged line.
    const out = tidySources([
      passage({ chunkId: "a", score: 0.01 }),
      passage({ chunkId: "b", score: 0.03 }),
    ]);

    expect(out[0]?.score).toBe(0.03);
  });

  it("test_no_scores_drops_nothing", () => {
    // Kills an implementation that divides or compares against a best of 0 and
    // empties the box when scores are missing.
    const out = tidySources([
      passage({ chunkId: "a", headingPath: ["1"], score: 0 }),
      passage({ chunkId: "b", headingPath: ["2"], score: 0 }),
      passage({ chunkId: "c", headingPath: ["3"], score: 0 }),
    ]);

    expect(out).toHaveLength(3);
  });

  it("test_empty_list_gives_empty_list", () => {
    // Kills an implementation that throws on an empty list (best of nothing).
    expect(tidySources([])).toEqual([]);
  });
});
