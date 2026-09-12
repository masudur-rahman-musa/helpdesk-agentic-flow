import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  AUDIT_RANGE_MESSAGE,
  AUDIT_VALIDATION_MESSAGE,
  AuditSearch,
} from "@/components/admin/audit-search";
import { QueryProvider } from "@/components/query-provider";
import { AUDIT_FILTER_MAX_LENGTHS, PAGE_SIZES } from "@/lib/audit-filters";
import { FAILURE_MESSAGES } from "@/lib/chat-errors";

/**
 * An earlier change §6, `audit-search-page.test.tsx`: the `/admin/audit` filter form,
 * the results table, the payload disclosure and the cursor stack, against a
 * stubbed `fetch`. jsdom, no agent-api, no backing service. (No basename
 * collision in the tree, a project rule: the neighbours are
 * `audit-proxy-routes.test.ts` and `audit-schema.test.ts`.)
 *
 * The unit under test is `AuditSearch` from
 * `web/components/admin/audit-search.tsx`, taking NO props and doing its own
 * read through TanStack Query (§2, 23d decision A), so
 * `app/admin/audit/page.tsx` stays a thin `AppShell` wrapper no case here
 * renders. Everything on this surface is a READ: no case posts anything,
 * because nothing here may.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 24ba's Deviations):
 * - the per-row disclosure is a `<details>`, found as `role="group"` named
 *   `Payload for event {seq}`. `details` maps to `group` in `aria-query`, and
 *   `group` takes its name from the AUTHOR and not from its contents in
 *   `dom-accessibility-api` — so the implementation owes that element an
 *   `aria-label` (or an `aria-labelledby` pointing at its `<summary>`); a bare
 *   `<summary>Payload for event 902</summary>` is not findable by this query.
 * - "collapsed" is the absence of the `open` attribute on that element.
 * - the results are a real `<table>`; its DATA rows are the `role="row"`
 *   elements holding at least one `role="cell"`, which excludes the header row
 *   (whose children are `columnheader`s) without depending on a `rowgroup`
 *   name.
 * - the `hash` cell is located by its `title`, and its text is asserted with
 *   `toBe` rather than `toHaveTextContent`, because a cell rendering the WHOLE
 *   64-character hash contains the 12-character prefix as a substring and
 *   would satisfy a containment assertion.
 * - which alert a failed read gets: a body carrying one of the design's eight
 *   codes is a TYPED REFUSAL and renders that code's sentence (§1's second
 *   unhappy path); everything else — a payload the parser refuses, a
 *   non-JSON body, or a `code` outside that vocabulary such as the proxy's own
 *   `UPSTREAM_UNAVAILABLE` — is a FAILED SEARCH and renders the generic
 *   sentence. §1 fixes both sentences and leaves the discrimination open, and
 *   only one reading makes the two criteria satisfiable together.
 * - `Page size` is a NATIVE select (§6): `role="combobox"` named `Page size`
 *   with `role="option"` children, 23f's deviation. The option VALUES are never
 *   asserted; what an option MEANS is the `limit` it sends, which is asserted
 *   on the request.
 * - dates are typed with `fireEvent.change`, the way `approvals-page.test.tsx`
 *   sets a long value: `user.type` into an `input[type="date"]` commits
 *   character by character and the intermediate values are not dates.
 *
 * Every fixture parses under `web/lib/audit-schema.ts` as 24a built it: the
 * nine `AuditEventRow` fields inside `{ events: [...], next_before_seq }`, with
 * `actor_ref`, `subject_id` and `next_before_seq` the three nullable ones.
 */

const ORIGIN = "http://localhost:3000";
const SEARCH_PATH = "/api/admin/audit/search";
const LOGIN_URL = "http://localhost:3000/login";
const READ_ALERT = "The audit log could not be read. Try again in a moment.";
const EMPTY_STATE = "No events match that search.";

/** 64-character hashes with no digits in them, so a row's `seq` assertion
 * cannot be satisfied by a hash that happens to contain those figures. */
const HASH_ONE = "ab".repeat(32);
const HASH_TWO = "cd".repeat(32);
const HASH_THREE = "ef".repeat(32);

const SEQ_ONE = 902;
const SEQ_TWO = 901;
const SEQ_THREE = 880;

/**
 * A nested payload with keys no other case uses (§5's sixth risk, 13b decision
 * E): a page reading named keys would render blanks for it, and one reshaping
 * it would not round-trip through `JSON.stringify(payload, null, 2)`.
 */
const PAYLOAD_ONE: Record<string, unknown> = {
  fake_outer: { fake_inner: ["fake-alpha", "fake-beta"], fake_flag: false },
  fake_note: "fake detail only this case knows",
};

/** A person's event: `actor_ref` and `subject_id` both present. */
const EVENT_ONE: Record<string, unknown> = {
  seq: SEQ_ONE,
  occurred_at: "2026-09-12T08:15:30.123456Z",
  actor_kind: "user",
  actor_ref: "fake-person-one",
  event_type: "grant.created",
  subject_type: "grant",
  subject_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
  payload: PAYLOAD_ONE,
  hash: HASH_ONE,
};

/** A system event: both nullable row fields are null on this one row. */
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

/** The only event of the SECOND page, so which page is on screen is visible. */
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

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

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

/**
 * The response a browser is handed after FOLLOWING the proxy's redirect to
 * `/login`: a 200 carrying the login page, with `redirected` true and `url`
 * pointing at where it ended up. `Response` has no constructor option for
 * either, so both are defined on the instance.
 */
function redirectedResponse(): Response {
  const response = new Response("<html>login</html>", {
    status: 200,
    headers: { "content-type": "text/html" },
  });
  Object.defineProperty(response, "redirected", { value: true, configurable: true });
  Object.defineProperty(response, "url", { value: LOGIN_URL, configurable: true });
  return response;
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

function callAt(urls: string[], index: number): string {
  const url = urls[index];
  if (url === undefined) {
    throw new Error(`expected a search call at index ${index}, found ${urls.length}`);
  }
  return url;
}

function lastSearch(): string {
  return callAt(searchCalls(), searchCalls().length - 1);
}

/** The query string of a call, without its leading `?`. */
function queryOf(url: string): string {
  return new URL(url, ORIGIN).search.replace(/^\?/, "");
}

/** A call's parameters as a plain object, so `toEqual` pins the WHOLE set. */
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

async function table(): Promise<HTMLElement> {
  return await within(await region()).findByRole("table", { name: "Audit events" });
}

/** The table's DATA rows: the ones holding cells rather than column headers. */
async function eventRows(): Promise<HTMLElement[]> {
  return within(await table())
    .getAllByRole("row")
    .filter((row) => within(row).queryAllByRole("cell").length > 0);
}

function at(elements: HTMLElement[], index: number): HTMLElement {
  const element = elements[index];
  if (element === undefined) {
    throw new Error(`expected an element at index ${index}, found ${elements.length}`);
  }
  return element;
}

async function searchButton(): Promise<HTMLElement> {
  return within(await region()).getByRole("button", { name: "Search" });
}

async function commit(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(await searchButton());
}

async function textControl(name: string): Promise<HTMLElement> {
  return await screen.findByRole("textbox", { name });
}

async function dateControl(name: string): Promise<HTMLElement> {
  return await screen.findByLabelText(name);
}

async function pageSizeSelect(): Promise<HTMLElement> {
  return await screen.findByRole("combobox", { name: "Page size" });
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
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes(SEARCH_PATH)) {
      return await searchAnswer(url);
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", {
    ...window.location,
    href: `${ORIGIN}/admin/audit`,
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AuditSearch — the first paint", () => {
  it("reads the search proxy with limit=50 and nothing else", async () => {
    // §1's first criterion as an EXACT query string, which is the only form
    // that pins it. Kills a page sending every empty control along
    // (`event_type=&subject_type=&...`): the route (24a) forwards each one and
    // `event_type=` is a filter nothing matches, so the auditor's first sight
    // of the log is an empty table. It also kills a page opening on some other
    // `limit`, and one carrying `before_seq` on the first paint, which would
    // start the auditor part-way down the log with no way to tell.
    renderSearch();

    await eventRows();
    expect(searchCalls()).toHaveLength(1);
    const url = callAt(searchCalls(), 0);
    expect(new URL(url, ORIGIN).pathname).toBe(SEARCH_PATH);
    expect(queryOf(url)).toBe("limit=50");
  });

  it("names the region Audit log", async () => {
    // a project rule's own subject, and the container every count in this
    // file is scoped to. Kills a page whose results are not inside a named
    // region: a screen-reader user then meets a table with no announced
    // context, and every `within(region)` query here would be picking things
    // out of the whole document instead.
    renderSearch();

    expect(await region()).toBeInTheDocument();
  });
});

describe("AuditSearch — the filter form", () => {
  const TEXT_FILTERS = [
    { control: "Event type", param: "event_type", value: "grant.created" },
    { control: "Subject type", param: "subject_type", value: "grant" },
    { control: "Subject id", param: "subject_id", value: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa" },
    { control: "Actor", param: "actor_ref", value: "fake-person-one" },
  ] as const;

  it.each(TEXT_FILTERS)("$control sends $param and no other parameter", async (filter) => {
    // §1's second criterion, one case per control, with the whole parameter set
    // asserted rather than the one parameter. Kills a control wired to the
    // wrong parameter — `Actor` sending `subject_id` returns rows about a
    // person rather than rows they caused, and both look like a working filter
    // — and kills a form that submits its EMPTY siblings alongside, which is
    // the fault that makes every filtered search return nothing. The `Search`
    // click is what pins "the applied filters, never the keystrokes" (§2): a
    // page reading on every keypress would have made more than two calls by
    // now. Arithmetic: search reads = 2, the first paint's
    // and this commit's.
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    await user.type(await textControl(filter.control), filter.value);
    await commit(user);

    await waitFor(() => {
      expect(searchCalls()).toHaveLength(2);
    });
    expect(paramsOf(lastSearch())).toEqual({ limit: "50", [filter.param]: filter.value });
  });

  const CAPS = [
    { control: "Event type", field: "event_type" },
    { control: "Subject type", field: "subject_type" },
    { control: "Subject id", field: "subject_id" },
    { control: "Actor", field: "actor_ref" },
  ] as const;

  it.each(CAPS)("caps $control at the $field column's length", async ({ control, field }) => {
    // Decision F and §1's third criterion, on the surface that produces the
    // string. Kills a form with no `maxLength` at all, where
    // a pasted 300-character subject id costs a round trip and comes back a
    // 400 with nothing pointing at which field was too long. The expectation is
    // READ from `AUDIT_FILTER_MAX_LENGTHS` rather than retyped, so this case
    // pins the WIRING — a `Subject id` box carrying `event_type`'s 64 is red
    // here — while `audit-filters.test.ts` pins the four numbers themselves.
    renderSearch();

    const input = await textControl(control);
    expect(input).toHaveAttribute("maxlength", String(AUDIT_FILTER_MAX_LENGTHS[field]));
  });

  it("sends From as midnight and To as the last second of its day", async () => {
    // §1's fourth criterion and decision C, as the two exact composed strings.
    // Kills a form posting the control's raw `2026-09-12`, which reaches a
    // `timestamptz` column as an instant whose meaning depends on the server's
    // zone, and kills a `To` composed at midnight, which excludes every event
    // of the last day the auditor asked about — the day most searches are
    // about, and a loss nothing on screen would show. The whole parameter set
    // is asserted, so a page that also sends the four empty text filters is red
    // here too.
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    fireEvent.change(await dateControl("From"), { target: { value: "2026-09-12" } });
    fireEvent.change(await dateControl("To"), { target: { value: "2026-09-13" } });
    await commit(user);

    await waitFor(() => {
      expect(searchCalls()).toHaveLength(2);
    });
    expect(paramsOf(lastSearch())).toEqual({
      limit: "50",
      occurred_from: "2026-09-12T00:00:00Z",
      occurred_to: "2026-09-13T23:59:59Z",
    });
  });

  it("refuses an inverted range in the browser and fires no read for it", async () => {
    // §5's fifth risk, and the reason §1 asserts the alert AND the absent call:
    // the sentence alone is green for a page that shows the message and sends
    // the search anyway, which is the shape a validation-after-submit handler
    // produces. Kills that, and kills a page with no pre-check at all, where
    // the tool answers `InvalidTimeRangeError` as a `VALIDATION` refusal — a
    // round trip and a rate-limit slot spent to say something the form already
    // knew. The call count is asserted against ONE, the first paint's, because
    // the paint happens before the click and its read is legitimate.
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    fireEvent.change(await dateControl("From"), { target: { value: "2026-09-13" } });
    fireEvent.change(await dateControl("To"), { target: { value: "2026-09-12" } });
    await commit(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(AUDIT_RANGE_MESSAGE);
    expect(searchCalls()).toHaveLength(1);
    expect(queryOf(callAt(searchCalls(), 0))).toBe("limit=50");
  });

  it("pins the inverted-range sentence exactly", async () => {
    // a project rule on one of the two strings this plan introduces. The case
    // above READS the constant, so it stays green for whatever the module
    // exports; only this literal fixes the words the auditor sees. Kills an
    // implementation that exports the symbol and fills it with the shared
    // `VALIDATION` sentence, or with a variant that does not name which of the
    // two dates is wrong.
    expect(AUDIT_RANGE_MESSAGE).toBe("The From date is after the To date.");
  });

  it("offers exactly the three page sizes under the name Page size", async () => {
    // Decision G as the control itself, its labels read from `PAGE_SIZES` so
    // the numbers are pinned once. Kills a select offering a size outside the
    // tool's `1`-`200` bound (the route refuses it and the auditor sees a
    // refusal for a choice the page offered), and kills a page with no such
    // control at all, which decision G rejects because 24bb exports what this
    // page holds and a 50-row CSV is a thin export. Scoped to the combobox, so
    // no other option-shaped thing on the page can satisfy it.
    renderSearch();

    const select = await pageSizeSelect();
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(PAGE_SIZES.map(String));
  });

  it("choosing 200 refetches the log with limit=200", async () => {
    // Two things at once. That a SECOND read happens at all is what pins the
    // page size being part of the query key (§6): a key ignoring it answers the
    // cached 50-row page and the auditor's choice does nothing for thirty
    // seconds, with no request to see. And the exact parameter set pins WHICH
    // read — a select wired to the label rather than the value would send
    // `limit=200 rows` or `limit=` and be refused upstream. Arithmetic: search
    // reads = 2, `limit=50` then `limit=200`.
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    const select = await pageSizeSelect();
    await user.selectOptions(select, within(select).getByRole("option", { name: "200" }));

    await waitFor(() => {
      expect(searchCalls()).toHaveLength(2);
    });
    expect(paramsOf(lastSearch())).toEqual({ limit: "200" });
  });
});

describe("AuditSearch — the results table", () => {
  it("renders one row per event, newest first, with every column", async () => {
    // §1's sixth criterion, one assertion per field. Kills a table rendering
    // only `events[0]`, and kills a page that re-sorts what the tool ordered —
    // 13b returns newest first and an auditor reading a log bottom-up loses the
    // one ordering the page exists to show. The 19-character assertion kills a
    // row printing the raw `2026-09-12T08:15:30.123456Z` (six digits of
    // microseconds in a column an auditor scans) and, because `.123456` is
    // asserted ABSENT, it cannot be satisfied by rendering the whole value. The
    // raw `event_type` is decision B: a hand-kept label map would be stale the
    // day a row adds an event type, and the literal is what the auditor pastes
    // back into the filter.
    renderSearch();

    const rows = await eventRows();
    expect(rows).toHaveLength(2);

    const first = at(rows, 0);
    expect(first).toHaveTextContent(String(SEQ_ONE));
    expect(first).toHaveTextContent("2026-09-12T08:15:30");
    expect(first.textContent ?? "").not.toContain(".123456");
    expect(first).toHaveTextContent("grant.created");
    expect(first).toHaveTextContent("user");
    expect(first).toHaveTextContent("fake-person-one");
    expect(first).toHaveTextContent("grant");
    expect(first).toHaveTextContent("aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa");

    const second = at(rows, 1);
    expect(second).toHaveTextContent(String(SEQ_TWO));
    expect(second).toHaveTextContent("review.opened");
    expect(second).toHaveTextContent("access_review");
    expect(screen.queryByText(EMPTY_STATE)).toBeNull();
  });

  it("renders the hash as a 12-character prefix carrying the whole hash as its title", async () => {
    // §1's sixth criterion at its two exact numbers. Kills a
    // row printing the full 64 characters, which pushes every other column off
    // the screen, and kills a truncated cell with NO title, where the one value
    // an auditor needs to compare against a chain verification is unreachable.
    // The text is asserted with `toBe`, because a cell rendering the whole hash
    // contains the prefix and would pass a containment check.
    renderSearch();

    const first = at(await eventRows(), 0);
    const cell = within(first).getByTitle(HASH_ONE);
    expect(cell.textContent).toBe(HASH_ONE.slice(0, 12));
    expect(cell.textContent).toHaveLength(12);
  });

  it("reads no actor and no subject id for the two null fields", async () => {
    // The null half of §1's sixth criterion, on the one row that carries both.
    // Kills a page interpolating the nulls — `null` or an empty cell where a
    // SYSTEM event's actor belongs — which is the shape every scheduler-written
    // event has, so the auditor cannot tell "no actor" from "the page failed to
    // render one". Scoped to the second row, because the first row HAS both
    // values and would hide the fault.
    renderSearch();

    const second = at(await eventRows(), 1);
    expect(second).toHaveTextContent("no actor");
    expect(second).toHaveTextContent("no subject id");
    expect(second.textContent ?? "").not.toContain("null");
  });

  it("holds each event's payload pretty-printed in a collapsed named disclosure", async () => {
    // §1's seventh criterion and §5's sixth risk. Kills a row rendering NAMED
    // payload keys: `payload` is an arbitrary object by design (13b decision
    // E), so this nested fixture — whose keys no other case uses — would come
    // out blank, and every event type but the one the implementer had in mind
    // would lose its detail. It also kills a disclosure rendered OPEN, which
    // would put a dozen lines of JSON between every two rows of a table an
    // auditor scans, and one whose name omits the `seq`, leaving fifty
    // identically named controls on a page.
    renderSearch();

    const first = at(await eventRows(), 0);
    const details = within(first).getByRole("group", { name: `Payload for event ${SEQ_ONE}` });
    expect(details).not.toHaveAttribute("open");
    expect(details.textContent ?? "").toContain(JSON.stringify(PAYLOAD_ONE, null, 2));
  });
});

describe("AuditSearch — paging through the log", () => {
  it("offers no Older when next_before_seq is null", async () => {
    // The end of the log. Kills an `Older` driven by the ROW COUNT — "a full
    // page means there is more" — which on this fixture would offer a cursor
    // the tool did not give, and the page would then ask for `before_seq=null`
    // or re-read the same rows for ever.
    renderSearch();

    await eventRows();
    expect(within(await region()).queryByRole("button", { name: "Older" })).toBeNull();
  });

  const CURSOR_PAGES = [
    { name: "a page of two rows", events: [EVENT_ONE, EVENT_TWO] },
    { name: "an EMPTY page", events: [] as Record<string, unknown>[] },
  ] as const;

  it.each(CURSOR_PAGES)("offers Older on $name with a cursor", async ({ events }) => {
    // §5's second risk, which is the one that loses evidence silently: 13b's
    // scope filter runs AFTER the read and `SCAN_CAP` is 500 rows per call, so
    // a narrow caller's page can be short or EMPTY while matching events sit
    // further back, and `next_before_seq` is the only way to reach them. The
    // empty row is the load-bearing one — a page hiding `Older` when it has no
    // rows to show tells an auditor the log is clean when it is not — and it
    // kills exactly the row-count-driven implementation the case above cannot.
    searchAnswer = () => page([...events], SEQ_TWO);
    renderSearch();

    expect(await within(await region()).findByRole("button", { name: "Older" })).toBeEnabled();
  });

  it("choosing Older reads the next page from that cursor", async () => {
    // §1's eighth criterion. That a second read happens at all pins the cursor
    // being in the query key (§6: `["admin", "audit", "search", filters,
    // beforeSeq]`); a key without it answers the cached first page and the
    // button does nothing. The exact parameter set pins the VALUE: a page
    // sending its own last row's `seq` minus one, or an offset, would skip or
    // repeat events around the boundary, and the rendered second page is what
    // kills a page that fetches and then shows the first page's rows anyway.
    stageTwoPages();
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    await user.click(within(await region()).getByRole("button", { name: "Older" }));

    await waitFor(() => {
      expect(searchCalls()).toHaveLength(2);
    });
    expect(paramsOf(lastSearch())).toEqual({ limit: "50", before_seq: String(SEQ_TWO) });
    await waitFor(async () => {
      expect(await eventRows()).toHaveLength(1);
    });
    expect(at(await eventRows(), 0)).toHaveTextContent(String(SEQ_THREE));
  });

  it("offers no Newer on the first page and offers it after one Older", async () => {
    // Decision D's stack, from the side a one-way pager passes every other case
    // with. Kills a `Newer` rendered always: on the first page it can only
    // re-read what is already on screen, or pop an empty stack and throw. And
    // it kills a pager with no `Newer` at all, where stepping back one page
    // means re-running the search from the top — which, with a filter typed in
    // by hand, is how an auditor loses their place.
    stageTwoPages();
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    expect(within(await region()).queryByRole("button", { name: "Newer" })).toBeNull();

    await user.click(within(await region()).getByRole("button", { name: "Older" }));

    expect(await within(await region()).findByRole("button", { name: "Newer" })).toBeEnabled();
  });

  it("choosing Newer pops the stack back to the first page", async () => {
    // The other half of §1's eighth criterion. Kills a `Newer` that PUSHES
    // another cursor instead of popping — the auditor would walk further down
    // the log while the control says they are going up — and kills one that
    // rebuilds the stack wrongly, leaving `Newer` on screen at the top. Every
    // call made after the click is asserted to be the FIRST page's url, so the
    // case holds whether the step back is a cache hit (which decision D says it
    // should be, the key being the same) or a refetch; the rendered rows are
    // what say which page is actually shown.
    stageTwoPages();
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    await user.click(within(await region()).getByRole("button", { name: "Older" }));
    await waitFor(async () => {
      expect(await eventRows()).toHaveLength(1);
    });
    const before = searchCalls().length;

    await user.click(within(await region()).getByRole("button", { name: "Newer" }));

    await waitFor(async () => {
      expect(await eventRows()).toHaveLength(2);
    });
    expect(at(await eventRows(), 0)).toHaveTextContent(String(SEQ_ONE));
    expect(within(await region()).queryByRole("button", { name: "Newer" })).toBeNull();
    for (const url of searchCalls().slice(before)) {
      expect(queryOf(url)).toBe("limit=50");
    }
  });

  it("committing a filter after paging clears the cursor stack", async () => {
    // §5's third risk, and the fault it ships: a cursor from one filter paging
    // another's results shows rows that match neither, with a `seq` boundary
    // taken from a search the auditor has already left. Kills a page that keeps
    // the stack across a commit — the read would carry BOTH the new
    // `event_type` and the old `before_seq`, which the exact parameter set
    // refuses — and kills one that leaves `Newer` on screen pointing at a page
    // of the previous search. Arithmetic: search reads = 3 (first paint,
    // `Older`, the commit).
    stageTwoPages();
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    await user.click(within(await region()).getByRole("button", { name: "Older" }));
    await waitFor(() => {
      expect(searchCalls()).toHaveLength(2);
    });

    await user.type(await textControl("Event type"), "grant.created");
    await commit(user);

    await waitFor(() => {
      expect(searchCalls()).toHaveLength(3);
    });
    expect(paramsOf(lastSearch())).toEqual({ limit: "50", event_type: "grant.created" });
    expect(within(await region()).queryByRole("button", { name: "Newer" })).toBeNull();
  });

  it("clears the cursor stack when the page size changes too", async () => {
    // a project rule's shape on §1's own sentence: "changing any filter OR
    // the page size clears the stack". Kills a page that clears on a filter
    // commit only — the fix applied at one of the two call sites — where
    // choosing 200 rows would re-read from the OLD cursor and silently hide
    // every event newer than it behind a control that says nothing about
    // paging.
    stageTwoPages();
    const user = userEvent.setup();
    renderSearch();

    await eventRows();
    await user.click(within(await region()).getByRole("button", { name: "Older" }));
    await waitFor(() => {
      expect(searchCalls()).toHaveLength(2);
    });

    const select = await pageSizeSelect();
    await user.selectOptions(select, within(select).getByRole("option", { name: "100" }));

    await waitFor(() => {
      expect(searchCalls()).toHaveLength(3);
    });
    expect(paramsOf(lastSearch())).toEqual({ limit: "100" });
    expect(within(await region()).queryByRole("button", { name: "Newer" })).toBeNull();
  });
});

describe("AuditSearch — nothing to show, and reads that fail", () => {
  it("renders the named empty state with no table and no alert", async () => {
    // §1's ninth criterion, the successful half. Kills a page rendering an
    // empty table under its heading, where "no events match" looks exactly like
    // a read that broke. The absent alert is the mutual exclusion: a page
    // showing both says two contradictory things about one answer, and an
    // auditor cannot tell whether to trust the emptiness.
    searchAnswer = () => page([], null);
    renderSearch();

    expect(await screen.findByText(EMPTY_STATE)).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Audit events" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  const FAILURES = [
    {
      name: "a payload the parser refuses",
      answer: (): Response =>
        jsonResponse({ events: [{ seq: SEQ_ONE }], next_before_seq: null }, 200),
    },
    {
      name: "the proxy's own 502",
      answer: (): Response => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
    {
      name: "a 500 carrying no JSON at all",
      answer: (): Response =>
        new Response("<html>bad gateway</html>", {
          status: 500,
          headers: { "content-type": "text/html" },
        }),
    },
  ] as const;

  it.each(FAILURES)(
    "$name renders the read alert, no table and no empty state",
    async (row) => {
      // All three rows kill one implementation from three directions: a fetcher
      // answering an empty page when the read fails. It would print "No events
      // match that search." over a log nobody could read — the one sentence on
      // this page an auditor acts on by concluding nothing happened — because a
      // proxy was down. The first row also kills a page that CASTS the body
      // instead of running `safeParse` (the rows draw with blank cells and no
      // error); the second kills one reading the body without checking the
      // status, a 502 envelope being valid JSON; the third kills one calling
      // `response.json()` unguarded, where the thrown parse error escapes as an
      // unhandled rejection instead of a sentence. `UPSTREAM_UNAVAILABLE` is
      // not one of the design's eight codes, so it is a failed search and not a
      // typed refusal.
      searchAnswer = row.answer;
      renderSearch();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(READ_ALERT);
      expect(screen.queryByRole("table", { name: "Audit events" })).toBeNull();
      expect(screen.queryByText(EMPTY_STATE)).toBeNull();
    },
    10_000,
  );
});

describe("AuditSearch — a refused search", () => {
  const REFUSALS = [
    { name: "FORBIDDEN", status: 403, code: "FORBIDDEN", sentence: FAILURE_MESSAGES.FORBIDDEN },
    {
      name: "TOOL_DISABLED",
      status: 503,
      code: "TOOL_DISABLED",
      sentence: FAILURE_MESSAGES.TOOL_DISABLED,
    },
    { name: "VALIDATION", status: 400, code: "VALIDATION", sentence: AUDIT_VALIDATION_MESSAGE },
  ] as const;

  it.each(REFUSALS)(
    "$name renders its own sentence and not the generic read alert",
    async (refusal) => {
      // §1's tenth criterion and decision H. Kills a page rendering the CODE at
      // the auditor ("TOOL_DISABLED" means nothing to the person reading it),
      // and kills one answering every failure with the generic sentence — a
      // caller holding no audit principal would be told to "try again in a
      // moment" for something no amount of retrying fixes, and a tripped kill
      // switch would read as an outage. The two shared sentences are READ from
      // `FAILURE_MESSAGES`, so a page inventing its own wording is red without
      // widening 22b's pinned map. The VALIDATION row is the override: it
      // asserts the local sentence AND the absence of the shared one, so
      // reusing the map — "That message could not be sent.", which is about a
      // chat message and says nothing true about a search — is red in both
      // directions.
      searchAnswer = () => jsonResponse({ code: refusal.code }, refusal.status);
      renderSearch();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(refusal.sentence);
      expect(screen.queryByText(READ_ALERT)).toBeNull();
      expect(screen.queryByText(EMPTY_STATE)).toBeNull();
      if (refusal.code === "VALIDATION") {
        expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.VALIDATION);
      }
    },
    10_000,
  );

  it("pins the VALIDATION override sentence exactly", async () => {
    // a project rule on the second string this plan introduces. The row above
    // READS the constant, so it stays green for any value the module chooses;
    // only this literal fixes it. Kills an implementation that exports the
    // symbol and fills it with the shared map's sentence, or with a reworded
    // variant that does not tell the auditor to look at their filters. Awaits
    // nothing — the constant is the whole subject.
    expect(AUDIT_VALIDATION_MESSAGE).toBe(
      "That search could not be run. Check the filters and try again.",
    );
    expect(AUDIT_VALIDATION_MESSAGE).not.toBe(FAILURE_MESSAGES.VALIDATION);
  });
});

describe("AuditSearch — an expired session", () => {
  it("follows the proxy's redirect to /login instead of rendering the log", async () => {
    // §1's last unhappy path, and the reason it is `response.redirected` and
    // never a status (22b decision E, a project rule): the browser has
    // ALREADY followed the proxy's redirect, so what arrives is a 200 carrying
    // the login page. Kills a fetcher checking `response.ok` or
    // `response.status === 401` — both see a successful response, the parser
    // then refuses the HTML, and the auditor sits on `/admin/audit` reading
    // "the audit log could not be read" for a session that merely expired,
    // which is the one failure a sign-in would have fixed. The absent alert is
    // what separates the two outcomes.
    searchAnswer = redirectedResponse;
    renderSearch();

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(screen.queryByRole("table", { name: "Audit events" })).toBeNull();
    expect(screen.queryByText(READ_ALERT)).toBeNull();
  });
});
