import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuditSearch } from "@/components/admin/audit-search";
import { QueryProvider } from "@/components/query-provider";
import { AUDIT_CSV_MIME } from "@/lib/audit-csv";

/**
 * An earlier change §6, `audit-export.test.tsx`: the `Export CSV` control inside
 * `/admin/audit`'s `Audit log` region — when it is there, what it builds and
 * what it downloads — against a stubbed `fetch`. jsdom, no agent-api, no
 * backing service. (No basename collision in the tree, a project rule: the
 * neighbours are `audit-csv.test.ts`, `audit-filters.test.ts`,
 * `audit-proxy-routes.test.ts`, `audit-schema.test.ts` and
 * `audit-search-page.test.tsx`.)
 *
 * The unit under test is `AuditSearch` from
 * `web/components/admin/audit-search.tsx`, taking NO props (24ba), because the
 * export is a pure function of the page state that component already holds
 * (decision A): no second read, no second cursor walk and no server route. The
 * BYTES are pinned in `audit-csv.test.ts`; what this file pins is the wiring —
 * which rows are handed to the file, and that the file reaches the disk.
 *
 * The three stubs are decision C's, named in the plan so no observation is
 * invented here: `URL.createObjectURL` and
 * `URL.revokeObjectURL`, and `HTMLAnchorElement.prototype.click` — the last of
 * which also stops jsdom answering "Not implemented: navigation" for a clicked
 * `<a download>`.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 24bb's Deviations):
 * - `vi.stubGlobal` takes a GLOBAL name, so the two object-URL functions are
 *   stubbed by replacing `URL` with a subclass carrying them as statics. The
 *   real `URL` constructor is left untouched, which matters because the
 *   component and the helpers here both build query strings with it.
 * - the clicked anchor is captured as the `this` of the click spy, not through
 *   `mock.contexts`, and the spy's implementation is replaced so no navigation
 *   is attempted.
 * - jsdom's `Blob` implements `slice`, `size` and `type` and NOT `text()`, so
 *   the exported bytes are read back with a `FileReader`.
 * - the expected file and the expected filename are LITERALS. Asserting
 *   `toCsv(rows)` or `csvFilename(rows)` here would be green for whatever those
 *   functions do, including nothing at all, which is exactly what §6 forbids.
 *
 * Every fixture parses under `web/lib/audit-schema.ts` as 24a built it: the
 * nine `AuditEventRow` fields inside `{ events: [...], next_before_seq }`.
 */

const ORIGIN = "http://localhost:3000";
const SEARCH_PATH = "/api/admin/audit/search";
const OBJECT_URL = "blob:http://localhost:3000/fake-object-url";

/** 64-character hashes, so a truncated export is visible in the file. */
const HASH_ONE = "ab".repeat(32);
const HASH_TWO = "cd".repeat(32);
const HASH_THREE = "ef".repeat(32);

const SEQ_ONE = 902;
const SEQ_TWO = 901;
const SEQ_THREE = 880;

/** A person's event: both nullable fields present, and a payload with a quote
 * in its serialised form, so the file's quoting is exercised end to end. */
const EVENT_ONE: Record<string, unknown> = {
  seq: SEQ_ONE,
  occurred_at: "2026-09-12T08:15:30.123456Z",
  actor_kind: "user",
  actor_ref: "fake-person-one",
  event_type: "grant.created",
  subject_type: "grant",
  subject_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
  payload: { fake_note: "fake detail" },
  hash: HASH_ONE,
};

/** A system event: `actor_ref` and `subject_id` both null on the same row. */
const EVENT_TWO: Record<string, unknown> = {
  seq: SEQ_TWO,
  occurred_at: "2026-09-11T17:04:05.000000Z",
  actor_kind: "system",
  actor_ref: null,
  event_type: "review.opened",
  subject_type: "access_review",
  subject_id: null,
  payload: {},
  hash: HASH_TWO,
};

/** The only event of the SECOND page, so which page was exported is visible. */
const EVENT_THREE: Record<string, unknown> = {
  seq: SEQ_THREE,
  occurred_at: "2026-09-10T06:00:00.000000Z",
  actor_kind: "user",
  actor_ref: "fake-person-two",
  event_type: "user.offboarded",
  subject_type: "user",
  subject_id: "bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb",
  payload: { fake_reason: "fake leaver" },
  hash: HASH_THREE,
};

/** The whole file for the first page, written out rather than computed. */
const EXPECTED_CSV =
  "seq,occurred_at,actor_kind,actor_ref,event_type,subject_type,subject_id,payload,hash\r\n" +
  "902,2026-09-12T08:15:30.123456Z,user,fake-person-one,grant.created,grant," +
  `aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa,"{""fake_note"":""fake detail""}",${HASH_ONE}\r\n` +
  `901,2026-09-11T17:04:05.000000Z,system,,review.opened,access_review,,{},${HASH_TWO}\r\n`;

const FILENAME_PAGE_ONE = "audit-902-901.csv";
const FILENAME_PAGE_TWO = "audit-880-880.csv";

const fetchStub = vi.fn<typeof fetch>();
const createObjectURL = vi.fn<typeof URL.createObjectURL>();
const revokeObjectURL = vi.fn<typeof URL.revokeObjectURL>();
const clicked: HTMLAnchorElement[] = [];

type SearchAnswer = (url: string) => Response | Promise<Response>;

let searchAnswer: SearchAnswer = () => page([EVENT_ONE, EVENT_TWO], null);

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** One page of the log, in `auditSearchSchema`'s envelope. */
function page(events: Record<string, unknown>[], nextBeforeSeq: number | null): Response {
  return jsonResponse({ events, next_before_seq: nextBeforeSeq }, 200);
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.toString();
  }
  return input.url;
}

function searchCalls(): string[] {
  return fetchStub.mock.calls
    .map(([input]) => urlOf(input))
    .filter((url) => url.includes(SEARCH_PATH));
}

/** A call's parameters as a plain object, the way 24ba's file reads them. */
function paramsOf(url: string): Record<string, string> {
  return Object.fromEntries(new URL(url, ORIGIN).searchParams.entries());
}

function renderSearch(): void {
  render(
    <QueryProvider>
      <AuditSearch />
    </QueryProvider>,
  );
}

async function region(): Promise<HTMLElement> {
  return await screen.findByRole("region", { name: "Audit log" });
}

/** The table's DATA rows: the ones holding cells rather than column headers. */
async function eventRows(): Promise<HTMLElement[]> {
  const table = await within(await region()).findByRole("table", { name: "Audit events" });
  return within(table)
    .getAllByRole("row")
    .filter((row) => within(row).queryAllByRole("cell").length > 0);
}

async function exportButton(): Promise<HTMLElement> {
  return within(await region()).getByRole("button", { name: "Export CSV" });
}

async function queryExportButton(): Promise<HTMLElement | null> {
  return within(await region()).queryByRole("button", { name: "Export CSV" });
}

/** The Blob the one object URL was created from. */
function exportedBlob(): Blob {
  const call = createObjectURL.mock.calls[0];
  if (call === undefined) {
    throw new Error("expected one URL.createObjectURL call, found none");
  }
  const [blob] = call;
  if (!(blob instanceof Blob)) {
    throw new Error("expected the object URL to be created from a Blob");
  }
  return blob;
}

/** jsdom's `Blob` has no `text()`, so the bytes come back through a reader. */
async function blobText(blob: Blob): Promise<string> {
  return await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("the blob could not be read"));
    reader.readAsText(blob);
  });
}

function clickedAnchor(): HTMLAnchorElement {
  const anchor = clicked[0];
  if (anchor === undefined) {
    throw new Error("expected one anchor click, found none");
  }
  return anchor;
}

/** A first page of two events with a cursor, and one further event behind it. */
function stageTwoPages(): void {
  searchAnswer = (url) =>
    paramsOf(url).before_seq === undefined
      ? page([EVENT_ONE, EVENT_TWO], SEQ_TWO)
      : page([EVENT_THREE], null);
}

beforeEach(() => {
  searchAnswer = () => page([EVENT_ONE, EVENT_TWO], null);
  clicked.length = 0;
  fetchStub.mockReset();
  createObjectURL.mockReset();
  createObjectURL.mockReturnValue(OBJECT_URL);
  revokeObjectURL.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes(SEARCH_PATH)) {
      return await searchAnswer(url);
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal(
    "URL",
    class StubUrl extends URL {
      static override createObjectURL = createObjectURL;
      static override revokeObjectURL = revokeObjectURL;
    },
  );
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicked.push(this);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AuditSearch — when Export CSV is offered", () => {
  it("offers no Export CSV before the first page arrives", async () => {
    // §1's first criterion, the state the page opens in. Kills a button
    // rendered beside the form rather than beside the RESULTS: choosing it
    // while the first read is still in flight exports the empty row list, and
    // an auditor gets a header-only file named for a range it does not hold,
    // with nothing on screen saying why. The region is asserted present first,
    // so an absent button cannot be satisfied by a page that rendered nothing
    // at all.
    searchAnswer = () => new Promise<Response>(() => {});
    renderSearch();

    expect(await region()).toBeInTheDocument();
    expect(await queryExportButton()).toBeNull();
  });

  it("offers no Export CSV when the page holds no events", async () => {
    // The same criterion at the boundary decision A's risk names: an empty
    // page. Kills a button guarded on `isSuccess` alone, which is true here —
    // the read worked and answered nothing — and would produce a file with a
    // header, no rows and a filename built from an empty seq range. §5 puts
    // this case in the plan precisely so an empty export is never produced at
    // all, rather than produced and explained.
    searchAnswer = () => page([], null);
    renderSearch();

    expect(await screen.findByText("No events match that search.")).toBeInTheDocument();
    expect(await queryExportButton()).toBeNull();
  });

  it("offers no Export CSV when the read failed", async () => {
    // The third absence, and the one a guard on `data` alone gets wrong: the
    // page holds the PREVIOUS answer or none, and the alert on screen says
    // the log could not be read. Kills a button that stays live through a
    // failure, where the auditor exports a file over a log the page just told
    // them it could not read — the worst possible moment to hand someone a
    // partial export that looks complete.
    searchAnswer = () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502);
    renderSearch();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent("The audit log could not be read. Try again in a moment.");
    expect(await queryExportButton()).toBeNull();
  }, 10_000);

  it("offers Export CSV inside the Audit log region once the page holds a row", async () => {
    // The present half of §1's first criterion, scoped to the region so a
    // control rendered somewhere else on the screen cannot satisfy it (standing
    // rule 55). Kills a page with no export at all — the design asks for it — and
    // kills one rendering it outside the region an auditor's screen reader
    // announces the log in, where it reads as a control belonging to nothing.
    searchAnswer = () => page([EVENT_ONE], null);
    renderSearch();

    await eventRows();
    expect(await exportButton()).toBeEnabled();
  });
});

describe("AuditSearch — what Export CSV downloads", () => {
  it("builds one object URL from a text/csv Blob holding the rows on screen", async () => {
    // §1's second, third and fourth criteria arriving as BYTES, and §6's rule
    // that the expectation is the literal file and never a second call to
    // `toCsv`. Kills a handler that stringifies the raw rows (`JSON.stringify`,
    // or `join(",")` over `Object.values`), which produces a file whose columns
    // do not line up with the header and whose null cells read `null`; kills
    // one exporting the parsed page's own field order rather than the wire
    // order; and kills a Blob built with no type or with `text/plain`, which
    // opens in a browser tab instead of saving. The single call is what pins
    // one file per choice: a handler creating a URL per ROW would leak an
    // object URL for every event and download the last one.
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    await user.click(await exportButton());

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blob = exportedBlob();
    expect(blob.type).toBe(AUDIT_CSV_MIME);
    expect(await blobText(blob)).toBe(EXPECTED_CSV);
  });

  it("clicks a named anchor at that url and revokes it afterwards", async () => {
    // The rest of decision C, as the three things a browser needs to save a
    // file. Kills an anchor with no `download` attribute, which NAVIGATES to
    // the blob url and shows the CSV as a page instead of saving it — the
    // auditor's export silently becomes a browsing session. Kills a `href`
    // built from anything but the created object URL (a data url, or the search
    // path), and kills a missing `revokeObjectURL`, which holds the whole file
    // in memory for the life of the document — on a 200-row export chosen
    // repeatedly, that is the page's memory growing with no cap. The filename
    // is the literal decision F fixes, because asserting `csvFilename(rows)`
    // here would be green for any name that function chooses, including none.
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    await user.click(await exportButton());

    const anchor = clickedAnchor();
    expect(anchor.getAttribute("download")).toBe(FILENAME_PAGE_ONE);
    expect(anchor.getAttribute("href")).toBe(OBJECT_URL);
    expect(revokeObjectURL).toHaveBeenCalledWith(OBJECT_URL);
  });

  it("exports the SECOND page's rows after paging to it", async () => {
    // Decision A's limit, made visible in the one place an auditor can see it
    // (§5's second risk). Kills an export built from the FIRST page the query
    // cache ever held — the shape a handler reading a captured `data` closure
    // or a stale ref produces — where paging three times and exporting hands
    // back the top of the log under a filename claiming the bottom of it, and
    // nothing on screen contradicts it. The filename is the assertion because
    // it carries the seq range: `audit-902-901.csv` here would be the fault,
    // and `audit-880-880.csv` is the page actually on screen.
    stageTwoPages();
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    await user.click(within(await region()).getByRole("button", { name: "Older" }));
    await waitFor(async () => {
      expect(await eventRows()).toHaveLength(1);
    });
    await user.click(await exportButton());

    expect(clickedAnchor().getAttribute("download")).toBe(FILENAME_PAGE_TWO);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(searchCalls()).toHaveLength(2);
  });
});
