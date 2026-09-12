import { describe, expect, it } from "vitest";

import { AUDIT_CSV_HEADER, AUDIT_CSV_MIME, csvFilename, toCsv } from "@/lib/audit-csv";
import { type AuditEventRow } from "@/lib/audit-schema";

/**
 * An earlier change §6, `audit-csv.test.ts`: the bytes of the exported file, over
 * `@/lib/audit-csv` and nothing else. No DOM, no fetch, no clock. (No basename
 * collision in the tree, a project rule: the neighbours are
 * `audit-filters.test.ts`, `audit-proxy-routes.test.ts`, `audit-schema.test.ts`
 * and `audit-search-page.test.tsx`.)
 *
 * The unit under test is the four symbols §6 names — `AUDIT_CSV_HEADER`,
 * `AUDIT_CSV_MIME`, `toCsv(events)` and `csvFilename(events)` — taking the
 * parsed `AuditEventRow`s of `lib/audit-schema.ts` and answering a string. The
 * component owes only the download (`audit-export.test.tsx`); every byte of the
 * file is pinned here, where a literal can hold it.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 24bb's Deviations):
 * - the varying value of the quoting and injection rows lives in `actor_ref`,
 *   one of the three fields §1 names as carrying text a user typed. Every other
 *   cell of the base row is a value needing neither the guard nor the quoting,
 *   so a row that is wrapped or prefixed is wrapped or prefixed for the reason
 *   the case is about.
 * - the expectation of each of those rows is the WHOLE file, composed from the
 *   row's literal cell by `fileWith`. §6 asks for "the exact cell text", and
 *   splitting a file whose cells may contain CR or LF on commas would be a
 *   second CSV parser inside the test.
 * - `toCsv` and `csvFilename` are never called to produce an expectation: every
 *   expected string is written out here, because an assertion computed by the
 *   code under test is green for whatever that code chose.
 * - `csvFilename([])` is NOT pinned: §1 says `Export CSV` is absent on an empty
 *   page, so no caller can reach it and a case here would invent a behaviour
 *   nothing asks for.
 *
 * The 64-character `HASH` is interpolated rather than retyped into each
 * expectation; it is a fixture constant of this file and not a value read back
 * off the implementation.
 */

/** A real-length hash, so a file holding the 12-character screen form differs. */
const HASH = "ab".repeat(32);

/**
 * The row every quoting and injection case varies one cell of. Nothing in it
 * needs quoting or the guard, so the expected file below is the base row with
 * exactly one cell changed.
 */
const BASE: AuditEventRow = {
  seq: 7,
  occurred_at: "2026-09-11T17:04:05.000000Z",
  actor_kind: "user",
  actor_ref: "fake-person",
  event_type: "grant.created",
  subject_type: "grant",
  subject_id: "fake-subject-id",
  payload: {},
  hash: HASH,
};

/** The base row with one `actor_ref`, which is where each case's value goes. */
function eventWith(actorRef: string): AuditEventRow {
  return { ...BASE, actor_ref: actorRef };
}

/** The whole file for one base row whose fourth cell is exactly `cell`. */
function fileWith(cell: string): string {
  return `${AUDIT_CSV_HEADER}\r\n7,2026-09-11T17:04:05.000000Z,user,${cell},grant.created,grant,fake-subject-id,{},${HASH}\r\n`;
}

describe("the header and the media type", () => {
  it("heads the file with the nine wire field names in the tool's order", () => {
    // §1's second criterion and a project rule and 39, as the one literal
    // that fixes it. Kills a header written in the TABLE's order or with the
    // table's own words (`Seq,Occurred,Event,Actor kind,Actor,...`, which is
    // the order `EventTable` renders and puts `event_type` third): a value
    // under a column named `Event` does not paste back into the `Event type`
    // filter, and an auditor comparing two exports column by column would be
    // comparing different columns. It also kills a header short by one — the
    // nine names are here as one string, so a file missing `payload` or `hash`
    // cannot satisfy it.
    expect(AUDIT_CSV_HEADER).toBe(
      "seq,occurred_at,actor_kind,actor_ref,event_type,subject_type,subject_id,payload,hash",
    );
  });

  it("declares the media type as text/csv with the utf-8 charset", () => {
    // a project rule on the second constant this plan introduces, and decision
    // G's other half: the charset lives HERE because no BOM is written. Kills a
    // bare `text/csv` (a browser saving the file then guesses the encoding, and
    // the guess is wrong for any non-ASCII actor a tenant types), and kills
    // `application/octet-stream`, which downloads as an unnamed binary.
    expect(AUDIT_CSV_MIME).toBe("text/csv;charset=utf-8");
  });
});

describe("toCsv — the wire values, not the screen's labels", () => {
  it("writes nulls as empty cells, the full timestamp and hash, and a compact payload", () => {
    // §1's third criterion and decision H, as ONE literal covering the whole
    // file. Four kills in one assertion, each of which is a real implementation
    // reusing what `EventTable` already renders: `no actor` and `no subject id`
    // in the two null cells (a spreadsheet filter would then read a literal
    // actor named "no actor", and the auditor cannot tell it from a real one);
    // `occurred_at.slice(0, 19)`, which drops the microseconds that order two
    // events inside one second; `hash.slice(0, 12)`, which makes the exported
    // hash uncheckable against any chain verification, the one thing this file
    // exists to allow; and `JSON.stringify(payload, null, 2)`, whose newlines
    // turn one payload into a block of lines inside a quoted cell. The trailing
    // CRLF is part of the literal, so a file ending bare is red here.
    const event: AuditEventRow = {
      seq: 901,
      occurred_at: "2026-09-11T17:04:05.000000Z",
      actor_kind: "system",
      actor_ref: null,
      event_type: "review.opened",
      subject_type: "access_review",
      subject_id: null,
      payload: { fake_note: "fake detail", fake_flag: false },
      hash: HASH,
    };

    expect(toCsv([event])).toBe(
      "seq,occurred_at,actor_kind,actor_ref,event_type,subject_type,subject_id,payload,hash\r\n" +
        "901,2026-09-11T17:04:05.000000Z,system,,review.opened,access_review,," +
        `"{""fake_note"":""fake detail"",""fake_flag"":false}",${HASH}\r\n`,
    );
  });

  it("writes the header and one CRLF for an empty list", () => {
    // §6's empty case. Kills a `toCsv` that answers "" for no rows — a file
    // with no header is not a CSV anyone can read a column out of — and kills
    // one that joins rows with a SEPARATOR rather than a terminator, which
    // shows up here as a header with no line ending at all. `Export CSV` is
    // absent on an empty page (§1), so this is the shape of the function and
    // not a path the button reaches.
    expect(toCsv([])).toBe(`${AUDIT_CSV_HEADER}\r\n`);
  });

  it("joins rows with CRLF and ends the file with one", () => {
    // §1's fourth criterion, from the side a whole-file literal cannot show.
    // Kills a file joined with bare LF: RFC 4180 says CRLF, and the split here
    // would answer one part holding both rows. The fourth part is the empty
    // string AFTER the trailing CRLF (hand count, a project rule: three
    // terminated lines split on their terminator give four parts), so a file
    // whose last row is unterminated gives three parts and is red.
    const first = eventWith("fake-person-one");
    const second: AuditEventRow = { ...BASE, seq: 6, actor_ref: "fake-person-two" };

    expect(toCsv([first, second]).split("\r\n")).toEqual([
      AUDIT_CSV_HEADER,
      `7,2026-09-11T17:04:05.000000Z,user,fake-person-one,grant.created,grant,fake-subject-id,{},${HASH}`,
      `6,2026-09-11T17:04:05.000000Z,user,fake-person-two,grant.created,grant,fake-subject-id,{},${HASH}`,
      "",
    ]);
  });
});

describe("toCsv — RFC 4180 quoting", () => {
  const QUOTING = [
    { name: "a comma", value: "fake,person", cell: '"fake,person"' },
    { name: "a double quote", value: 'fake"person', cell: '"fake""person"' },
    { name: "a line feed", value: "fake\nperson", cell: '"fake\nperson"' },
    { name: "a carriage return", value: "fake\rperson", cell: '"fake\rperson"' },
    { name: "none of the four", value: "fake person", cell: "fake person" },
  ] as const;

  it.each(QUOTING)("wraps a cell containing $name exactly as RFC 4180 says", ({ value, cell }) => {
    // §1's fourth criterion, one row per character plus the bare row that is
    // the rule's other side. The comma row kills the obvious
    // `values.join(",")`, which turns one nine-column row into ten columns and
    // shifts every later cell under the wrong header — the fault that makes an
    // export quietly wrong rather than visibly broken. The quote row kills an
    // implementation that wraps but does not DOUBLE the inner quote, which ends
    // the cell early at the first `"` and is unreadable to every parser. The LF
    // and CR rows kill wrapping written for the comma alone: an unquoted
    // newline inside a cell splits one event into two rows, and a reader counts
    // more events than the log holds. The bare row kills a writer that quotes
    // EVERYTHING, which is legal CSV but makes every one of these four cases
    // pass while pinning nothing about the rule.
    expect(toCsv([eventWith(value)])).toBe(fileWith(cell));
  });
});

describe("toCsv — the formula injection guard", () => {
  const GUARD = [
    { name: "an equals sign", value: "=fake", cell: "'=fake" },
    { name: "a plus", value: "+fake", cell: "'+fake" },
    { name: "a minus", value: "-fake", cell: "'-fake" },
    { name: "an at sign", value: "@fake", cell: "'@fake" },
    { name: "a tab", value: "\tfake", cell: "'\tfake" },
    { name: "a carriage return", value: "\rfake", cell: '"\'\rfake"' },
  ] as const;

  it.each(GUARD)("prefixes a cell beginning with $name", ({ value, cell }) => {
    // §1's fifth criterion and decision B, one case per character (standing
    // rule 1's inclusive boundary, from the accepted side). Kills a guard
    // written for `=` alone, which is the shape everyone writes first and which
    // leaves `+HYPERLINK(...)`, `-2+3+cmd|...` and `@SUM(...)` live in a
    // spreadsheet an auditor opens; a tenant's own `actor_ref` is where that
    // text comes from, so the guard is the only thing between a typed username
    // and an executed cell. The carriage-return row also kills a guard applied
    // AFTER the quoting: the apostrophe has to be inside the wrapper, and this
    // literal has it there.
    expect(toCsv([eventWith(value)])).toBe(fileWith(cell));
  });

  const NOT_FIRST = [
    { name: "an equals sign", value: "fake=value", cell: "fake=value" },
    { name: "a plus", value: "fake+value", cell: "fake+value" },
    { name: "a minus", value: "fake-value", cell: "fake-value" },
    { name: "an at sign", value: "fake@value", cell: "fake@value" },
    { name: "a tab", value: "fake\tvalue", cell: "fake\tvalue" },
    { name: "a carriage return", value: "fake\rvalue", cell: '"fake\rvalue"' },
  ] as const;

  it.each(NOT_FIRST)("leaves $name alone anywhere but the first character", ({ value, cell }) => {
    // The refused side of the same boundary, and the half that keeps the guard
    // honest. Kills a guard testing `value.includes("-")`, which prefixes every
    // hyphenated event type, every ISO timestamp and every UUID `subject_id` in
    // the file with an apostrophe — an export that no longer matches the log it
    // came from, and a `subject_id` that no longer pastes back into the filter.
    // The carriage-return row is the load-bearing one: it is WRAPPED either
    // way, so the only difference between it and its sibling above is the
    // apostrophe, and removing the first-character test alone flips it.
    expect(toCsv([eventWith(value)])).toBe(fileWith(cell));
  });

  it("prefixes and wraps a cell that needs both, in that order", () => {
    // §5's first risk, spelled out in §6 as one literal. Kills the two orders
    // that are each defensible on their own and wrong together: quoting first
    // gives `'"=a,b"`, whose leading apostrophe is outside the cell and which
    // every parser reads as a malformed field, and prefixing a value that was
    // already wrapped gives the same. The expected string has the apostrophe
    // INSIDE the quotes, immediately before the `=`, which is the only
    // arrangement a spreadsheet both parses as one cell and refuses to
    // evaluate.
    expect(toCsv([eventWith("=a,b")])).toBe(fileWith('"\'=a,b"'));
  });
});

describe("csvFilename", () => {
  it("names the file for the seq range of the rows it holds", () => {
    // §1's sixth criterion and decision F, as the literal §6 fixes. Kills a
    // name built from a clock (`audit-2026-09-13.csv`), which says nothing
    // about WHICH slice of the log the file holds and collides the moment an
    // auditor exports two pages on one day — the exact case decision A's
    // page-at-a-time export makes routine. It also kills a name built from the
    // row COUNT or from the first row alone, neither of which tells a reader
    // where the export stops, which is the one thing that makes a partial
    // export visible rather than inferred.
    const rows = [
      { ...BASE, seq: 42 },
      { ...BASE, seq: 30 },
      { ...BASE, seq: 11 },
    ];

    expect(csvFilename(rows)).toBe("audit-42-11.csv");
  });

  it("names a one-row page with that row's seq at both ends", () => {
    // The boundary of the same rule, where newest and oldest are the same
    // event. Kills a name built as `first.seq`-`last.seq` on the assumption
    // that they differ — an implementation reaching for `rows[1]` answers
    // `audit-42-undefined.csv` here — and kills one that emits a bare
    // `audit-42.csv` for the single-row case, which is a second naming shape
    // for a reader to learn.
    expect(csvFilename([{ ...BASE, seq: 42 }])).toBe("audit-42-42.csv");
  });
});
