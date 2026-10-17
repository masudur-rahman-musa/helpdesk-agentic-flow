import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PoliciesBoard } from "@/features/admin/policies/components/policies-board";
import { QueryProvider } from "@/components/layout/query-provider";
import { FAILURE_MESSAGES } from "@/lib/failure-messages";
import {
  LIVE_VERSION_TEXT,
  NO_CHUNKS_TEXT,
  NO_LIVE_VERSION_TEXT,
  NOT_LIVE_VERSION_TEXT,
  POLICIES_EMPTY_TEXT,
  POLICIES_READ_MESSAGE,
} from "@/features/admin/policies/format";

/**
 * `policies-board.test.tsx`: the read-only `/admin/policies` board
 * — one region per ingested document, one row per version — against a stubbed
 * `fetch`. jsdom, no agent-api, no backing service. (The neighbours are `policies-schema.test.ts`
 * and
 * `policies-proxy-routes.test.ts`, same folder.)
 *
 * The unit under test is `PoliciesBoard` from
 * `web/src/features/admin/policies/components/policies-board.tsx`, taking NO props and doing its own
 * read through TanStack Query, so `app/admin/policies/page.tsx`
 * stays a thin `AppShell` wrapper no case here renders. The surface is a READ
 * plus ONE control: an `Actions` column carrying a re-ingest
 * per distinct version, and three cases below click it. Everything else here is
 * still a read, and the control's own behaviour — the arming, the body, the
 * alerts, the redirect — belongs to `tests/policies-reingest.test.tsx`; what
 * this file owes is WHICH rows carry a control and what the board does with the
 * callback it passes down.
 *
 * The accessible names are fixed: the board
 * region `Policies`, the heading `Policy documents`, one region per document
 * named by its `slug`, each holding a table named `Versions of <slug>`, whose
 * seven column headers are `Version`, `Status`, `Ingested`, `Chunks`, `Source`,
 * `Digest`, `Actions`, and one control per distinct version named
 * `Re-ingest <slug> <version>`.
 * The implementation owes each `<section>` and each `<table>` an `aria-label`,
 * because neither role takes its name from its contents.
 *
 * Shapes this file fixes:
 * - the component's exported name is `PoliciesBoard`; only the file name was fixed.
 * - "no query string and no body" is asserted as: exactly one call, whose
 *   pathname is `/api/admin/policies`, whose `search` is empty, whose method is
 *   `GET` (absent counts, `fetch`'s own default) and whose `body` is
 *   `undefined`. The relative-versus-absolute form of the URL is NOT fixed.
 * - a version row's first six cells are read BY POSITION in the header order
 *   above, which the appended seventh leaves exactly where they were:
 *   `role="cell"` carries no name of its own, so `within(row)` alone cannot tell
 *   the `Chunks` cell from the `Version` one — which is the swap several of
 *   these cases exist to catch.
 * - the document ORDER is read off each document region's `aria-label`, because
 *   nothing else exposes an accessible name as a LIST: the implementation owes
 *   the attribute itself rather than an `aria-labelledby` pointing at the
 *   heading, which would name the region after the title instead of the slug.
 * - rows are identified by their `Version` cell, the one value every fixture
 *   here gives a distinct string to inside its document.
 * - `No live version` is asserted anywhere inside its DOCUMENT region rather
 *   than in a named element: the document shows the line and nothing says what
 *   say what holds it.
 * - the digest cell is located by its `title`, the way `audit-search-page.test
 *   .tsx::renders the hash as a 12-character prefix ...` locates the hash cell,
 *   and its text is asserted with `toBe` because a cell rendering all 64
 *   characters CONTAINS the 12.
 * - an expired session is `response.redirected` with `url` on the INSTANCE and
 *   `location` stubbed through `vi.stubGlobal`.
 * - two rules about a document with NO current version disagree for a
 *   document whose `versions` is EMPTY: such a document has no current version
 *   (so one rule asks for `No live version`) and another says it renders no
 *   extra sentence at all. The second wins here — it is the rule written
 *   FOR that document — and `::renders a document with no versions as its
 *   heading alone` pins the absence.
 *
 * Every fixture parses under `web/src/features/admin/policies/schema.ts`: the
 * three `PolicyDocumentRow` fields inside `{ documents: [...] }`, with the six
 * `PolicyVersionRow` fields nested and nothing nullable. That parser is pinned
 * by `tests/policies-schema.test.ts` and only CONSUMED here. All names are
 * obviously fake.
 */

const ORIGIN = "http://localhost:3000";
const BOARD_PATH = "/api/admin/policies";
/** the control posts here. It CONTAINS `BOARD_PATH`, so paths are compared
 * exactly and never with `includes`. */
const REINGEST_PATH = "/api/admin/policies/reingest";
const LOGIN_URL = "http://localhost:3000/login";

/** The six READ columns, in header order; the control column is a seventh. */
const VERSION_CELL = 0;
const STATUS_CELL = 1;
const INGESTED_CELL = 2;
const CHUNKS_CELL = 3;
const SOURCE_CELL = 4;
const DIGEST_CELL = 5;

/**
 * Two fake 64-character digests whose first twelve characters differ, so a
 * board rendering the wrong version's digest is red rather than accidentally
 * right. Built with `repeat` rather than typed out, because a literal one
 * character short would make the length premise below wrong in the one
 * direction nothing else notices.
 */
const SHA_LIVE = `1111111111aa${"b".repeat(52)}`;
const SHA_OLD = `2222222222cc${"d".repeat(52)}`;

const INGESTED_AT = "2026-09-13T14:05:09+00:00";
const INGESTED_RENDERED = "2026-09-13 14:05 UTC";

function version(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    version: "v2",
    is_current: true,
    ingested_at: INGESTED_AT,
    chunk_count: 7,
    source_path: "fake-corpus/fake-access-policy/v2.md",
    sha256: SHA_LIVE,
    ...overrides,
  };
}

function document(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    slug: "fake-access-policy",
    title: "Fake Access Policy",
    versions: [version({})],
    ...overrides,
  };
}

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

type BoardAnswer = () => Response | Promise<Response>;

let boardAnswer: BoardAnswer = () => board([document({})]);

/** What the control gets back, for the three cases below that click it. */
let reingestAnswer: BoardAnswer = () => reingestResult();

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The corpus in `policyDocumentsBoardSchema`'s envelope. */
function board(documents: Record<string, unknown>[]): Response {
  return jsonResponse({ documents }, 200);
}

/** A re-ingest answer in `policyReingestResultSchema`'s shape: all eight. */
function reingestResult(overrides: Record<string, unknown> = {}): Response {
  return jsonResponse(
    {
      slug: "fake-access-policy",
      version: "v2",
      created: true,
      chunk_count: 7,
      is_current: true,
      sha256: SHA_LIVE,
      ingested_at: INGESTED_AT,
      summary: "Fake summary, tests only.",
      ...overrides,
    },
    200,
  );
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

function pathOf(input: RequestInfo | URL): string {
  return new URL(urlOf(input), ORIGIN).pathname;
}

function boardCalls(): Parameters<typeof fetch>[] {
  return fetchStub.mock.calls.filter(([input]) => pathOf(input) === BOARD_PATH);
}

/** Every POST the control made, read the same exact way. */
function reingestCalls(): Parameters<typeof fetch>[] {
  return fetchStub.mock.calls.filter(([input]) => pathOf(input) === REINGEST_PATH);
}

function at<T>(values: readonly T[], index: number): T {
  const value = values[index];
  if (value === undefined) {
    throw new Error(`expected an entry at index ${index}, found ${values.length}`);
  }
  return value;
}

function renderBoard(): void {
  render(
    <QueryProvider>
      <PoliciesBoard />
    </QueryProvider>,
  );
}

async function boardRegion(): Promise<HTMLElement> {
  return await screen.findByRole("region", { name: "Policies" });
}

/** One document's region, named by its slug. */
async function documentRegion(slug: string): Promise<HTMLElement> {
  return await within(await boardRegion()).findByRole("region", { name: slug });
}

/** Every document region's accessible name, in the order they are painted. */
async function renderedSlugs(): Promise<string[]> {
  return within(await boardRegion())
    .getAllByRole("region")
    .map((region) => region.getAttribute("aria-label") ?? "");
}

async function versionsTable(slug: string): Promise<HTMLElement> {
  return await within(await documentRegion(slug)).findByRole("table", {
    name: `Versions of ${slug}`,
  });
}

/** Every table on the surface, whatever its name: what "no table" means here. */
function anyTables(): HTMLElement[] {
  return screen.queryAllByRole("table");
}

/** One document's version rows: the ones holding cells rather than headers. */
async function versionRows(slug: string): Promise<HTMLElement[]> {
  return within(await versionsTable(slug))
    .getAllByRole("row")
    .filter((candidate) => within(candidate).queryAllByRole("cell").length > 0);
}

/** One row's cell at a column index, with its text collapsed. */
function cellText(rowElement: HTMLElement, column: number): string {
  const cells = within(rowElement).getAllByRole("cell");
  return (at(cells, column).textContent ?? "").trim();
}

/** The rendered `Version` cell of every row of a document, in paint order. */
async function renderedVersions(slug: string): Promise<string[]> {
  return (await versionRows(slug)).map((rowElement) => cellText(rowElement, VERSION_CELL));
}

/** The one row of a document whose `Version` cell names this version. */
async function rowFor(slug: string, versionName: string): Promise<HTMLElement> {
  const rows = await versionRows(slug);
  const match = rows.filter((rowElement) => cellText(rowElement, VERSION_CELL) === versionName);
  if (match.length !== 1) {
    throw new Error(`expected exactly one row for ${versionName}, found ${match.length}`);
  }
  return at(match, 0);
}

beforeEach(() => {
  boardAnswer = () => board([document({})]);
  reingestAnswer = () => reingestResult();
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const path = pathOf(input);
    if (path === REINGEST_PATH) {
      return await reingestAnswer();
    }
    if (path === BOARD_PATH) {
      return await boardAnswer();
    }
    throw new Error(`unexpected fetch to ${urlOf(input)}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", {
    ...window.location,
    href: `${ORIGIN}/admin/policies`,
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PoliciesBoard — the read it makes", () => {
  it("reads the proxy once on paint, as a GET with no query and no body", async () => {
    // As the call's whole argument list.
    // Kills a board that sends the tenant, the principal or a slug filter along
    // (`?tenant=...`): which corpus this caller sees is the token's tenant,
    // decided behind the MCP boundary, and a page that names one is trusting the caller's tenant
    // written into the browser. Kills a POST — this surface holds no control at
    // all, so nothing here may write. Kills a board that reads on a click
    // instead of on paint (no call at all by the time the table exists), and one
    // firing twice per paint, which doubles the tool's work for a screen nobody
    // interacted with.
    renderBoard();

    await versionRows("fake-access-policy");
    expect(boardCalls()).toHaveLength(1);
    const [input, init] = at(boardCalls(), 0);
    const url = new URL(urlOf(input), ORIGIN);
    expect(url.pathname).toBe(BOARD_PATH);
    expect(url.search).toBe("");
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.body).toBeUndefined();
  });
});

describe("PoliciesBoard — the shape of the board", () => {
  const ZETA = document({
    slug: "fake-zeta-policy",
    title: "Fake Zeta Policy",
    versions: [version({ version: "v1" })],
  });
  const MID = document({
    slug: "fake-mid-policy",
    title: "Fake Mid Policy",
    versions: [version({ version: "v1" })],
  });
  const ALPHA = document({
    slug: "fake-alpha-policy",
    title: "Fake Alpha Policy",
    versions: [version({ version: "v1" })],
  });

  it("renders the board region and its heading", async () => {
    // The container half, and the anchor every other
    // case in this file scopes itself to. Kills a board that paints its
    // documents as loose sections with no landmark at all, where a screen
    // reader user has no way to jump to the corpus and where `within(board)`
    // below would be scoping to the whole page. The heading is asserted
    // separately from the region's name because they are different strings by
    // the wording — `Policies` names the region, `Policy documents` is the
    // visible heading — and a board reusing one for both is red here.
    renderBoard();

    expect(await boardRegion()).toBeInTheDocument();
    expect(
      await within(await boardRegion()).findByRole("heading", { name: "Policy documents" }),
    ).toBeInTheDocument();
  });

  const ORDERS: ReadonlyArray<{ name: string; documents: Record<string, unknown>[] }> = [
    { name: "reverse alphabetical", documents: [ZETA, MID, ALPHA] },
    { name: "neither sort order", documents: [MID, ALPHA, ZETA] },
  ];

  it.each(ORDERS)(
    "renders a payload in $name order exactly as it arrived",
    async ({ documents }) => {
      // With two payload orders because ONE proves
      // nothing: a board sorting ascending would pass a payload that happened to
      // arrive sorted. The first row kills an ascending re-sort by slug, the second row kills a
      // descending one and any "group by prefix" arrangement — its three slugs
      // are in neither alphabetical direction. The order on the wire is the
      // tool's own (`slug` ascending), and this component is not
      // the place that decides what the board contains or in what order: a
      // document ingested by a later plan must not be able to land somewhere an
      // admin does not look.
      boardAnswer = () => board(documents);
      renderBoard();

      const expected = documents.map((entry) => entry.slug);
      await waitFor(async () => {
        expect(await renderedSlugs()).toHaveLength(expected.length);
      });
      expect(await renderedSlugs()).toEqual(expected);
    },
  );

  it("names each document's region by its slug and its table by that slug too", async () => {
    // On the two names a document owes. Kills a
    // board that names every document region the same thing (`Document`), which
    // leaves three identically named regions a screen reader user cannot tell
    // apart, and kills one whose tables are all named `Versions` — with three
    // documents on screen, the versions of the wrong policy are then
    // indistinguishable from the right one's. The title is asserted as the
    // region's HEADING rather than as its name, which is the split: the slug
    // names the region, the title is what a person reads.
    boardAnswer = () => board([ZETA, ALPHA]);
    renderBoard();

    const planted: ReadonlyArray<{ slug: string; title: string }> = [
      { slug: "fake-zeta-policy", title: "Fake Zeta Policy" },
      { slug: "fake-alpha-policy", title: "Fake Alpha Policy" },
    ];
    for (const { slug, title } of planted) {
      const region = await documentRegion(slug);
      expect(await within(region).findByRole("heading", { name: title })).toBeInTheDocument();
      expect(await versionsTable(slug)).toBeInTheDocument();
    }
  });

  it("renders the seven column headers in order", async () => {
    // As one exact sequence, and
    // the guard every cell case below leans on: those are positional, so a
    // column inserted anywhere must be red HERE rather than silently making six
    // cell cases read the wrong column. Kills a board that folds the live
    // marker into the version cell (a rejected shape), which
    // leaves no cell that can say whether a version is live without re-reading
    // the version string, and kills one that reorders `Source` and `Digest` so
    // the two 12-character strings in the last columns are swapped past
    // noticing.
    //
    // `Actions` is asserted LAST, which is why it is "appended": a board putting
    // the new column anywhere else keeps seven headers and shifts every one of
    // the six positional cell assertions in this file onto its neighbour, so a
    // digest would be read where a source path is. That is the kill this
    // sequence carries that a containment check on `Actions` would not.
    renderBoard();

    const headers = within(await versionsTable("fake-access-policy"))
      .getAllByRole("columnheader")
      .map((header) => (header.textContent ?? "").trim());
    expect(headers).toEqual([
      "Version",
      "Status",
      "Ingested",
      "Chunks",
      "Source",
      "Digest",
      "Actions",
    ]);
  });

  it("renders a document's versions in the payload's own order", async () => {
    // Over version strings that sort DIFFERENTLY as
    // strings than as versions: `v2`, `v10`, `v9` on the wire is neither
    // `["v10", "v2", "v9"]` (string ascending), nor `["v9", "v2", "v10"]`
    // (string descending), nor `["v10", "v9", "v2"]` (the tool's own order,
    // recomputed here), so every re-sort a component could plausibly apply is
    // red. The wire order is `ingested_at` descending then `version` descending and is decided
    // behind the MCP boundary; a board that
    // re-derives it from the version STRING reports `v9` as newer than `v10`,
    // which is exactly the two-digit release this page will meet first.
    boardAnswer = () =>
      board([
        document({
          versions: [
            version({ version: "v2", is_current: false }),
            version({ version: "v10", is_current: true }),
            version({ version: "v9", is_current: false }),
          ],
        }),
      ]);
    renderBoard();

    await waitFor(async () => {
      expect(await renderedVersions("fake-access-policy")).toHaveLength(3);
    });
    expect(await renderedVersions("fake-access-policy")).toEqual(["v2", "v10", "v9"]);
  });
});

describe("PoliciesBoard — what a version row shows", () => {
  const TWO_VERSIONS = document({
    versions: [
      version({ version: "v2", is_current: true, chunk_count: 7, sha256: SHA_LIVE }),
      version({
        version: "v1",
        is_current: false,
        chunk_count: 0,
        sha256: SHA_OLD,
        source_path: "fake-corpus/fake-access-policy/v1.md",
      }),
    ],
  });

  it("renders Live on the current version and Not live on the others", async () => {
    // The per-row half. Kills a board written
    // `row.is_current && LIVE_VERSION_TEXT`, which paints a BLANK cell for every
    // superseded version — "never a blank cell", and the reading an
    // admin cannot tell from a cell that failed to draw. Kills the swap, which
    // would report a superseded version as the one the retriever answers from.
    // Asserted with `toBe` and not containment, because "Live" is a substring of
    // "Not live" and a containment check cannot separate them. Both rows are in
    // ONE payload, so a board that renders the marker from the FIRST row's
    // value and repeats it down the column is red on the second row.
    boardAnswer = () => board([TWO_VERSIONS]);
    renderBoard();

    expect(cellText(await rowFor("fake-access-policy", "v2"), STATUS_CELL)).toBe(LIVE_VERSION_TEXT);
    expect(cellText(await rowFor("fake-access-policy", "v1"), STATUS_CELL)).toBe(
      NOT_LIVE_VERSION_TEXT,
    );
  });

  it("renders the ingested instant as its date and minute in UTC", async () => {
    // The cell goes through the EXISTING
    // `formatSwitchInstant`, so the literal here is that function's contract
    // written out. Kills a board interpolating `ingested_at` raw, which shows
    // `2026-09-13T14:05:09+00:00` — the `T`, the seconds and an offset — in a
    // column read at a glance. Kills one built on `new Date(...)` with
    // `toLocaleString`, which answers a different DAY either side of midnight in
    // a non-UTC browser, so two admins comparing the same ingest disagree about
    // when it landed. Kills a second copy of the slice written into
    // `lib/policies-format.ts` only if that copy is wrong; the ONE
    // implementation is the point and is not
    // observable here.
    boardAnswer = () => board([document({})]);
    renderBoard();

    expect(cellText(await rowFor("fake-access-policy", "v2"), INGESTED_CELL)).toBe(
      INGESTED_RENDERED,
    );
  });

  it("renders a chunk_count of zero with its marker and a non-zero count without one", async () => {
    // Both directions in one payload.
    // The zero row kills a board that renders the bare number: a `0` in a column
    // of numbers reads as "small", when it is the one value on this surface that
    // means the ingest registered a document and wrote no span — a version the
    // retriever can never answer from, invisible any other way. It also kills a
    // board written `chunk_count || NO_CHUNKS_TEXT`, which drops the `0` itself
    // and leaves an admin unable to tell a zero from a missing field.
    //
    // The `7` row is what makes the marker mean something: it kills a board that
    // prints `No chunks ingested` on every row, which would pass the zero
    // assertion alone while telling an admin the whole corpus is empty. Its cell
    // text is asserted with `toBe`, so a marker appended to a healthy count is
    // red.
    boardAnswer = () => board([TWO_VERSIONS]);
    renderBoard();

    const emptyRow = await rowFor("fake-access-policy", "v1");
    expect(cellText(emptyRow, CHUNKS_CELL)).toContain("0");
    expect(emptyRow).toHaveTextContent(NO_CHUNKS_TEXT);

    const healthyRow = await rowFor("fake-access-policy", "v2");
    expect(cellText(healthyRow, CHUNKS_CELL)).toBe("7");
    expect(healthyRow).not.toHaveTextContent(NO_CHUNKS_TEXT);
  });

  it("renders source_path verbatim", async () => {
    // The value is corpus-relative, and
    // this cell prints what the tool sent and nothing else. Kills a board that
    // prettifies the path — a basename alone (`v1.md`) loses which corpus
    // directory the version came from, which is the only handle a re-ingest
    // will have — and kills one that prefixes a host or a mount point of
    // its own, which would put a fact about the ingest machine in the browser.
    // `toBe` and not containment, so an added prefix or suffix is red.
    boardAnswer = () => board([TWO_VERSIONS]);
    renderBoard();

    expect(cellText(await rowFor("fake-access-policy", "v2"), SOURCE_CELL)).toBe(
      "fake-corpus/fake-access-policy/v2.md",
    );
    expect(cellText(await rowFor("fake-access-policy", "v1"), SOURCE_CELL)).toBe(
      "fake-corpus/fake-access-policy/v1.md",
    );
  });

  it("renders the digest as twelve characters carrying the whole sha256 as its title", async () => {
    // The two exact numbers. Kills a row printing all 64 characters, which costs the other five
    // columns their width on every version row. Kills a truncated cell with NO
    // `title`, where the one value an admin compares against an ingest log is
    // unreachable — the hover being what the 64 characters were traded for
    // for. The cell is LOCATED by the full digest, so a board that puts the
    // prefix in the attribute and the whole value in the text fails to find it
    // at all, and its text is asserted with `toBe`, because a cell rendering the
    // whole digest CONTAINS the prefix and would pass a containment check.
    // Scoped to the row whose digest is `SHA_LIVE`, so a board rendering the
    // first version's digest on every row is red on the second.
    boardAnswer = () => board([TWO_VERSIONS]);
    renderBoard();

    expect(SHA_LIVE).toHaveLength(64);
    expect(SHA_OLD).toHaveLength(64);

    const liveRow = await rowFor("fake-access-policy", "v2");
    const liveCell = within(liveRow).getByTitle(SHA_LIVE);
    expect(liveCell.textContent).toBe(SHA_LIVE.slice(0, 12));
    expect(liveCell.textContent).toHaveLength(12);
    expect(cellText(liveRow, DIGEST_CELL)).toBe(SHA_LIVE.slice(0, 12));

    const oldRow = await rowFor("fake-access-policy", "v1");
    expect(within(oldRow).getByTitle(SHA_OLD).textContent).toBe(SHA_OLD.slice(0, 12));
  });
});

describe("PoliciesBoard — a document with no live version", () => {
  const NO_CURRENT = document({
    slug: "fake-half-landed-policy",
    title: "Fake Half Landed Policy",
    versions: [
      version({ version: "v2", is_current: false }),
      version({ version: "v1", is_current: false }),
    ],
  });

  it("shows the line on a document whose versions are all superseded, and not on one that is live", async () => {
    // Second half, the document-level half —
    // the state this page exists to show. Kills a board that marks only the LIVE
    // row (a rejected badge), which says nothing at all when there is
    // no live row: the half-landed ingest then reads as an ordinary document
    // with a few versions, and the retriever answering from nothing is invisible
    // here.
    //
    // The second document is what stops the line from being unconditional: a
    // board printing `No live version` in every document region would pass the
    // first assertion while telling an admin the whole corpus is dead. Both are
    // in ONE payload and each assertion is scoped to its own region, so the
    // presence and the absence cannot be satisfied by one global sentence.
    boardAnswer = () => board([NO_CURRENT, document({})]);
    renderBoard();

    expect(await documentRegion("fake-half-landed-policy")).toHaveTextContent(NO_LIVE_VERSION_TEXT);
    expect(await documentRegion("fake-access-policy")).not.toHaveTextContent(NO_LIVE_VERSION_TEXT);
  });
});

describe("PoliciesBoard — nothing to show", () => {
  it("renders the named empty state with no table and no alert", async () => {
    // The successful half. Kills a
    // board rendering empty headers, where "no documents ingested" looks exactly
    // like a read that broke. The absent ALERT is the mutual exclusion: a page
    // showing both says two contradictory things about one answer, and an admin
    // cannot tell whether to trust the emptiness. The sentence is read from the
    // constant `policies-format.test.ts` pins verbatim, because it is the one an
    // admin acts on by concluding the tenant has no policy corpus at all.
    boardAnswer = () => board([]);
    renderBoard();

    expect(await screen.findByText(POLICIES_EMPTY_TEXT)).toBeInTheDocument();
    expect(anyTables()).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders a document with no versions as its region and heading alone", async () => {
    // Unreachable through the tool —
    // every `policy_documents` row IS a version — and pinned so the shape is
    // decided rather than discovered at runtime. Kills a board that maps
    // `versions[0]` without checking, which throws on `undefined.version` and
    // takes the WHOLE board down with it, so one impossible document hides every
    // real one. Kills one that renders an empty table under six headers, a row
    // of column names with nothing beneath.
    //
    // The absent `No live version` is this file's reading of a contradiction in
    // the spec (see the header): such a document has no current version, so one rule
    // asks for the line while another says no extra sentence. The
    // criterion written for THIS document wins, and this assertion is what makes
    // the choice checkable rather than accidental. The absent empty state is the
    // other half: a corpus of one document is not an empty corpus.
    boardAnswer = () => board([document({ versions: [] })]);
    renderBoard();

    const region = await documentRegion("fake-access-policy");
    expect(
      await within(region).findByRole("heading", { name: "Fake Access Policy" }),
    ).toBeInTheDocument();
    expect(anyTables()).toHaveLength(0);
    expect(region).not.toHaveTextContent(NO_LIVE_VERSION_TEXT);
    expect(screen.queryByText(POLICIES_EMPTY_TEXT)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("PoliciesBoard — reads that fail", () => {
  const FAILURES: ReadonlyArray<{ name: string; answer: BoardAnswer }> = [
    {
      name: "a payload the parser refuses",
      answer: () =>
        jsonResponse(
          {
            documents: [
              {
                slug: "fake-access-policy",
                title: "Fake Access Policy",
                versions: [
                  {
                    version: "v2",
                    is_current: true,
                    ingested_at: INGESTED_AT,
                    source_path: "fake-corpus/fake-access-policy/v2.md",
                    sha256: SHA_LIVE,
                  },
                ],
              },
            ],
          },
          200,
        ),
    },
    {
      name: "a 500 carrying no JSON at all",
      answer: () =>
        new Response("<html>gateway</html>", {
          status: 500,
          headers: { "content-type": "text/html" },
        }),
    },
    {
      name: "a typed body carrying UPSTREAM_UNAVAILABLE",
      answer: () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ];

  it.each(FAILURES)(
    "$name renders the read message, no table and no empty state",
    async ({ answer }) => {
      // The LAST sentence of the failure path, the
      // refusal-versus-failure discriminator, from three directions, all killing one
      // implementation: a
      // board that answers an EMPTY CORPUS when the read fails. It would print
      // "No policy documents have been ingested." over a tenant whose corpus
      // nobody could read, which is the main risk and the one sentence on this
      // page an admin acts on by concluding the ingest never ran.
      //
      // The first row is a version missing `chunk_count` alone, so it also kills a page
      // that CASTS the body instead of running `safeParse`: the row would draw
      // with an undefined count, a version on screen whose span total — the one
      // fact separating an ingest from a registration — is silently blank.
      // The second row kills one calling `response.json()` unguarded, where the parse
      // error escapes as an unhandled rejection instead of a sentence.
      // The third row is the discriminator itself: `UPSTREAM_UNAVAILABLE` is NOT one of
      // the eight typed error codes, so it is a failed read and not a typed refusal,
      // and a board answering `failureMessage(code)` here shows "Something went
      // wrong. Nothing was changed.", which is red against this constant. It
      // also kills a board reading the body without checking the status, a 502
      // envelope being valid JSON.
      boardAnswer = answer;
      renderBoard();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(POLICIES_READ_MESSAGE);
      expect(anyTables()).toHaveLength(0);
      expect(screen.queryByText(POLICIES_EMPTY_TEXT)).toBeNull();
    },
    10_000,
  );
});

describe("PoliciesBoard — a refused read", () => {
  const REFUSALS: ReadonlyArray<{
    name: string;
    status: number;
    code: string;
    sentence: string;
  }> = [
    { name: "FORBIDDEN", status: 403, code: "FORBIDDEN", sentence: FAILURE_MESSAGES.FORBIDDEN },
    {
      name: "TOOL_DISABLED",
      status: 503,
      code: "TOOL_DISABLED",
      sentence: FAILURE_MESSAGES.TOOL_DISABLED,
    },
  ];

  it.each(REFUSALS)(
    "$name renders its own sentence in an alert, with no table",
    async ({ status, code, sentence }) => {
      // One case per REACHABLE code. Kills a board
      // rendering the CODE at the admin ("TOOL_DISABLED" means nothing to the
      // person reading it), and kills one answering every failure with
      // `POLICIES_READ_MESSAGE` — a caller who is neither `it_admin` nor
      // `platform_admin` would be told to "try again in a moment" for something
      // no amount of retrying fixes, and `list_policy_documents` held down by a
      // switch would read as a broken proxy rather than as the deliberate stop
      // it is. Both sentences are READ from `FAILURE_MESSAGES` rather than
      // retyped, so a board inventing its own wording is red without widening
      // the pinned map. The absent read message is what separates this path
      // from the three failures above: a board mapping every
      // thrown error to one sentence is green on that file and red here.
      boardAnswer = () => jsonResponse({ code }, status);
      renderBoard();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(sentence);
      expect(screen.queryByText(POLICIES_READ_MESSAGE)).toBeNull();
      expect(screen.queryByText(POLICIES_EMPTY_TEXT)).toBeNull();
      expect(anyTables()).toHaveLength(0);
    },
    10_000,
  );
});

describe("PoliciesBoard — an expired session", () => {
  it("follows the proxy's redirect to /login instead of rendering a board", async () => {
    // And the reason it is
    // `response.redirected` and never a status: the browser has ALREADY followed the proxy's
    // redirect, so what
    // arrives is a 200 carrying the login page. Kills a fetcher checking
    // `response.ok` or `response.status === 401` — both see a successful
    // response, the parser then refuses the HTML, and the admin sits on
    // `/admin/policies` reading "the policy documents could not be read" for a
    // session that merely expired.
    //
    // The absent EMPTY STATE is the worse half and the one a copied shape gets
    // wrong (a wrong copy of the shape): `readWire` answers `null` here,
    // and a board mapping that `null` to `{ documents: [] }` would tell somebody
    // who is not signed in that this tenant has ingested no policy at all. All
    // three absences are asserted because the third answer is exactly what this
    // case exists to pin.
    boardAnswer = redirectedResponse;
    renderBoard();

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(anyTables()).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(POLICIES_EMPTY_TEXT)).toBeNull();
  });
});

describe("PoliciesBoard — the re-ingest control it carries (plan 31b §6)", () => {
  /**
   * A third fake digest, so the document below holds TWO `v1` rows that differ
   * only in their bytes — the state `register_document_version` writes when an
   * edited file is re-ingested under the same version string,
   * and the state criteria 2 and 12 both read.
   */
  const SHA_OTHER = `3333333333ee${"f".repeat(52)}`;

  const REPEATED = document({
    versions: [
      version({ version: "v2", is_current: true, sha256: SHA_LIVE }),
      version({
        version: "v1",
        is_current: false,
        sha256: SHA_OTHER,
        source_path: "fake-corpus/fake-access-policy/v1.md",
      }),
      version({
        version: "v1",
        is_current: false,
        sha256: SHA_OLD,
        source_path: "fake-corpus/fake-access-policy/v1.md",
      }),
    ],
  });

  function controlName(slug: string, versionName: string): string {
    return `Re-ingest ${slug} ${versionName}`;
  }

  it("renders one re-ingest control per distinct version of a document", async () => {
    // The tool re-ingests a version
    // DIRECTORY, so two registered rows sharing a version string are one
    // re-ingestable thing: a control on each is a duplicate accessible name
    // doing an identical act, which a screen reader user cannot tell apart and
    // which spends a second minted key on the same directory. The count is
    // asserted over the whole document region, so a board rendering a control
    // on every row (three here) is red, and so is one rendering a single
    // per-document control acting on the live version — that shape could never
    // refresh a superseded version's file, which is exactly the file an admin
    // edits before promoting it.
    //
    // WHICH `v1` row carries it is the other half: the control sits on the
    // FIRST occurrence in the payload's own order, the newest row of that
    // version (versions arrive `ingested_at` descending). The
    // fixture puts that occurrence at index 1 rather than at the top of the
    // table, so a board that special-cases the first ROW of the table passes
    // nothing here.
    boardAnswer = () => board([REPEATED]);
    renderBoard();

    const region = await documentRegion("fake-access-policy");
    await waitFor(async () => {
      expect(await versionRows("fake-access-policy")).toHaveLength(3);
    });
    expect(within(region).getAllByRole("button")).toHaveLength(2);
    expect(
      within(region).getByRole("button", { name: controlName("fake-access-policy", "v1") }),
    ).toBeInTheDocument();
    expect(
      within(region).getByRole("button", { name: controlName("fake-access-policy", "v2") }),
    ).toBeInTheDocument();

    const rows = await versionRows("fake-access-policy");
    expect(
      within(at(rows, 1)).getByRole("button", { name: controlName("fake-access-policy", "v1") }),
    ).toBeInTheDocument();
    expect(within(at(rows, 2)).queryAllByRole("button")).toHaveLength(0);
  });

  it("renders both rows of a repeated version string", async () => {
    // `register_document_version`
    // matches on the DIGEST, so a changed re-ingest writes a SECOND row under
    // the same version string and the read answers both. Keyed by the bare
    // `version`, React sees two children under one key: it warns, and — the
    // part no warning describes — it carries one row's component state onto the
    // other, so the `v1` control's armed flag and in-flight flag would land on
    // a row the admin never touched. The kill is BOTH halves at once: three
    // rows still rendered (a board that deduplicated by version would show
    // two, hiding the row whose digest is the one on disk) and no `key`
    // complaint on `console.error`, which is the only place React says it.
    // The mutation is what makes the state reachable, which is why the
    // case lives with it and not with the read.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    boardAnswer = () => board([REPEATED]);
    renderBoard();

    await waitFor(async () => {
      expect(await versionRows("fake-access-policy")).toHaveLength(3);
    });
    expect(await renderedVersions("fake-access-policy")).toEqual(["v2", "v1", "v1"]);
    const complaints = consoleError.mock.calls
      .map((call) => call.map((part) => String(part)).join(" "))
      .filter((message) => message.toLowerCase().includes("key"));
    expect(complaints).toEqual([]);
    consoleError.mockRestore();
  });

  it("passes the board's invalidation down rather than exposing its query key", async () => {
    // From the board's side, and the
    // division. The control holds no knowledge of the cache at all: the board
    // owns `BOARD_KEY` and hands down an `onChanged`, so the two modules do not
    // import each other and a later change to the key cannot leave a control
    // invalidating a query that no longer exists — a silent staleness, with the
    // page showing pre-ingest counts and no error anywhere. The kill is the
    // SECOND read: a board that renders the control and passes nothing makes
    // exactly one, and the admin would have to reload by hand to see whether
    // the file they made live actually landed. Arithmetic:
    // board GETs = 2 (paint, then the invalidation), POSTs = 1. Everything the
    // control does with that answer is `tests/policies-reingest.test.tsx`'s.
    boardAnswer = () => board([document({})]);
    const user = userEvent.setup();
    renderBoard();

    const row = await rowFor("fake-access-policy", "v2");
    await user.click(
      within(row).getByRole("button", { name: controlName("fake-access-policy", "v2") }),
    );

    await waitFor(() => {
      expect(reingestCalls()).toHaveLength(1);
    });
    await waitFor(() => {
      expect(boardCalls()).toHaveLength(2);
    });
  });
});
