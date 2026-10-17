import { describe, expect, it } from "vitest";

import { formatIsoDate, formatSwitchInstant } from "@/lib/date-format";

describe("formatIsoDate", () => {
  it("test_format_iso_date_first_ten", () => {
    // Catches a formatter that returns the whole timestamp, or cuts at the
    // wrong index (nine or eleven characters).
    expect(formatIsoDate("2026-09-13T14:05:09+00:00")).toBe("2026-09-13");
  });

  it("test_format_iso_date_short_unchanged", () => {
    // Catches a formatter that pads, throws on or blanks a string under ten
    // characters instead of returning it as is.
    expect(formatIsoDate("2026-09")).toBe("2026-09");
    expect(formatIsoDate("")).toBe("");
  });

  it("test_format_iso_date_exactly_ten", () => {
    // Catches an off-by-one that drops the last character of a bare date.
    expect(formatIsoDate("2026-09-13")).toBe("2026-09-13");
  });
});

describe("formatSwitchInstant", () => {
  it("test_switch_instant_moved_and_exported", () => {
    // Catches a move that forgets to export formatSwitchInstant from lib, so
    // the kill switch and policies screens lose their time word.
    expect(typeof formatSwitchInstant).toBe("function");
    expect(formatSwitchInstant("2026-09-12T08:00:00+00:00")).toEqual(expect.any(String));
  });
});
