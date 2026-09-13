import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KillSwitchBoard } from "@/components/admin/kill-switch-board";
import { QueryProvider } from "@/components/query-provider";
import { FAILURE_MESSAGES } from "@/lib/chat-errors";
import { KILL_SWITCH_READ_MESSAGE } from "@/lib/killswitch-format";

/**
 * An earlier change §6, `killswitch-board.test.tsx`: the read-only `/admin/killswitch`
 * board — one row per registered tool, both layers per row — against a stubbed
 * `fetch`. jsdom, no agent-api, no backing service. (No basename collision in
 * the tree, a project rule: the neighbours are `killswitch-schema.test.ts`
 * and `killswitch-proxy-routes.test.ts`.)
 *
 * The unit under test is `KillSwitchBoard` from
 * `web/components/admin/kill-switch-board.tsx`, taking NO props and doing its
 * own read through TanStack Query (§2, decision A), so
 * `app/admin/killswitch/page.tsx` stays a thin `AppShell` wrapper no case here
 * renders. Everything on this surface is a READ: no case posts anything,
 * because nothing here may — the toggle is an earlier change's.
 *
 * The accessible names are the plan's own (§6): the region `Kill switches`, the
 * table `Tool kill switches`, and the four column headers `Tool`, `Status`,
 * `Platform-wide`, `This tenant`. The implementation owes the `<section>` and
 * the `<table>` an `aria-label` each, because neither role takes its name from
 * its contents.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 24da's Deviations):
 * - "the call's whole argument list" is asserted as: exactly one call, whose
 *   pathname is `/api/admin/killswitch`, whose `search` is the empty string,
 *   whose method is `GET` (absent counts, `fetch`'s own default) and whose
 *   `body` is `undefined`. The relative-versus-absolute form of the URL is NOT
 *   fixed, because the proxy path is what §1 pins and nothing else observes it.
 * - a row's four cells are asserted BY POSITION in the header order above:
 *   `role="cell"` carries no name of its own, and `within(row)` alone cannot
 *   tell the `Platform-wide` cell from the `This tenant` one — which is the
 *   swap this file exists to catch.
 * - the `Switch value unreadable` text is asserted anywhere in its ROW rather
 *   than in a named cell: §1 says the row carries it and does not say which
 *   cell holds it.
 * - the `Status` cell's text is asserted with `toBe` and not
 *   `toHaveTextContent`, so a cell reading "Not stopped" cannot satisfy
 *   "Stopped".
 * - an expired session is `response.redirected` with `url` on the INSTANCE and
 *   `location` stubbed through `vi.stubGlobal`.
 *
 * Every fixture parses under `web/lib/killswitch-schema.ts` as 24cb built it:
 * the six `KillSwitchRow` fields inside `{ switches: [...] }`, with
 * `global_state` and `tenant_state` the two nullable ones. That parser is
 * pinned by `tests/killswitch-schema.test.ts` and only CONSUMED here.
 */

const ORIGIN = "http://localhost:3000";
const BOARD_PATH = "/api/admin/killswitch";
const LOGIN_URL = "http://localhost:3000/login";
const EMPTY_STATE = "No tools are registered.";
const UNREADABLE_TEXT = "Switch value unreadable";
const PROTECTED_TEXT = "Protected";
const NO_SWITCH = "No switch";

/** The four columns, in the order §1 fixes them. */
const TOOL_CELL = 0;
const STATUS_CELL = 1;
const GLOBAL_CELL = 2;
const TENANT_CELL = 3;

/**
 * The two layers, each with its own reason, its own actor and its own instant,
 * so a board rendering the platform switch in the tenant column is red rather
 * than accidentally right. Obviously fake actors.
 */
const GLOBAL_STATE: Record<string, unknown> = {
  reason: "Fake platform-wide stop.",
  actor: "fake-admin-one",
  at: "2026-09-12T08:00:00+00:00",
};
const GLOBAL_RENDERED = "2026-09-12 08:00 UTC";

const TENANT_STATE: Record<string, unknown> = {
  reason: "Fake tenant-only stop.",
  actor: "fake-admin-two",
  at: "2026-09-11T17:30:45+00:00",
};
const TENANT_RENDERED = "2026-09-11 17:30 UTC";

function row(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    tool: "request_access",
    protected: false,
    stopped: false,
    global_state: null,
    tenant_state: null,
    unreadable: false,
    ...overrides,
  };
}

/** Held down by BOTH layers: the row every layer-cell assertion reads. */
const ROW_BOTH_LAYERS = row({
  tool: "request_access",
  stopped: true,
  global_state: GLOBAL_STATE,
  tenant_state: TENANT_STATE,
});

/** A running tool with neither layer set: the `No switch` row. */
const ROW_RUNNING = row({ tool: "grant_access" });

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

type BoardAnswer = () => Response | Promise<Response>;

let boardAnswer: BoardAnswer = () => board([ROW_BOTH_LAYERS, ROW_RUNNING]);

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The board in `killSwitchBoardSchema`'s envelope. */
function board(switches: Record<string, unknown>[]): Response {
  return jsonResponse({ switches }, 200);
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

function boardCalls(): Parameters<typeof fetch>[] {
  return fetchStub.mock.calls.filter(([input]) => urlOf(input).includes(BOARD_PATH));
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
      <KillSwitchBoard />
    </QueryProvider>,
  );
}

async function region(): Promise<HTMLElement> {
  return await screen.findByRole("region", { name: "Kill switches" });
}

async function table(): Promise<HTMLElement> {
  return await within(await region()).findByRole("table", { name: "Tool kill switches" });
}

function missingTable(): HTMLElement | null {
  return screen.queryByRole("table", { name: "Tool kill switches" });
}

/** The table's DATA rows: the ones holding cells rather than column headers. */
async function switchRows(): Promise<HTMLElement[]> {
  return within(await table())
    .getAllByRole("row")
    .filter((candidate) => within(candidate).queryAllByRole("cell").length > 0);
}

/** One row's cell at a column index, with its text collapsed. */
function cellText(rowElement: HTMLElement, column: number): string {
  const cells = within(rowElement).getAllByRole("cell");
  return (at(cells, column).textContent ?? "").trim();
}

/** The rendered `Tool` cell of every data row, in the order they are painted. */
async function renderedTools(): Promise<string[]> {
  return (await switchRows()).map((rowElement) => cellText(rowElement, TOOL_CELL));
}

/** The one data row whose `Tool` cell names this tool. */
async function rowFor(tool: string): Promise<HTMLElement> {
  const rows = await switchRows();
  const match = rows.filter((rowElement) => cellText(rowElement, TOOL_CELL).includes(tool));
  if (match.length !== 1) {
    throw new Error(`expected exactly one row for ${tool}, found ${match.length}`);
  }
  return at(match, 0);
}

beforeEach(() => {
  boardAnswer = () => board([ROW_BOTH_LAYERS, ROW_RUNNING]);
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes(BOARD_PATH)) {
      return await boardAnswer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", {
    ...window.location,
    href: `${ORIGIN}/admin/killswitch`,
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("KillSwitchBoard — the read it makes", () => {
  it("reads the proxy once on paint, as a GET with no query and no body", async () => {
    // §1's first criterion and decision A, as the call's whole argument list.
    // Kills a board that sends the tenant, the principal or a tool filter along
    // (`?tenant=...`): which board this caller sees is the token's tenant,
    // decided behind the MCP boundary, and a page that names one is a project rule
    // written into the browser. Kills a POST — nothing on this surface may
    // write, and 24db's toggle is the only verb that ever will. Kills a board
    // that reads on a click instead of on paint (no call at all by the time the
    // table exists), and one that fires twice per paint, which doubles the
    // tool's Redis MGET for a screen nobody interacted with.
    renderBoard();

    await switchRows();
    expect(boardCalls()).toHaveLength(1);
    const [input, init] = at(boardCalls(), 0);
    const url = new URL(urlOf(input), ORIGIN);
    expect(url.pathname).toBe(BOARD_PATH);
    expect(url.search).toBe("");
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.body).toBeUndefined();
  });

  it("names the region Kill switches and the table Tool kill switches", async () => {
    // a project rule's own subject, and the container every count in this
    // file is scoped to. Kills a board whose table sits outside any named
    // region — a screen-reader user then meets a grid with no announced
    // context — and kills an unnamed `<table>`, which on a page that later
    // grows a second one (24db's answer line) becomes unaddressable. Every
    // `within(region)` query here would otherwise be picking elements out of
    // the whole document.
    renderBoard();

    expect(await table()).toBeInTheDocument();
  });
});

describe("KillSwitchBoard — the shape of the board", () => {
  it("renders the five column headers in order", async () => {
    // §1's third criterion as an exact sequence. Kills a board that merges the
    // two layers into one column — decision C's whole point is that an admin
    // clearing the tenant switch has to be told the global one still holds the
    // tool down, and a merged column cannot say so — and kills one that renames
    // `This tenant` to something an admin reads as platform-wide. The order is
    // asserted because every cell assertion in this file is positional, so a
    // swapped pair of headers must be red HERE rather than silently making the
    // layer cases assert the wrong columns.
    renderBoard();

    const headers = within(await table())
      .getAllByRole("columnheader")
      .map((header) => (header.textContent ?? "").trim());
    expect(headers).toEqual(["Tool", "Status", "Platform-wide", "This tenant", "Actions"]);
  });

  const ORDERS: ReadonlyArray<{ name: string; tools: readonly string[] }> = [
    { name: "reverse alphabetical", tools: ["request_access", "grant_access", "approve_request"] },
    { name: "neither sort order", tools: ["grant_access", "approve_request", "request_access"] },
  ];

  it.each(ORDERS)("renders a payload in $name order exactly as it arrived", async ({ tools }) => {
    // §1's second criterion, with two payload orders because ONE proves
    // nothing: a board sorting ascending would pass a payload that happened to
    // arrive sorted. Row 1 kills an ascending sort, row 2 kills a descending
    // one and any "stopped tools first" grouping — its three tools are in
    // neither alphabetical direction. The order on the wire is the registry's
    // (24ca), and this component is not the place that decides what the board
    // contains or in what order: a tool added by a later plan must not be able
    // to land somewhere an operator does not look.
    boardAnswer = () => board(tools.map((tool) => row({ tool })));
    renderBoard();

    await waitFor(async () => {
      expect(await renderedTools()).toHaveLength(tools.length);
    });
    expect(await renderedTools()).toEqual([...tools]);
  });

  it("renders one row per switches entry and no more", async () => {
    // The count, scoped to the named table. Kills a board that drops a row it
    // cannot render (a tool with neither layer set, which is the common case)
    // and one that renders a spare filler row: the board is the only surface
    // from which a stopped tool can be seen, so a missing line is a tool nobody
    // knows is down.
    renderBoard();

    expect(await switchRows()).toHaveLength(2);
  });
});

describe("KillSwitchBoard — the status cell", () => {
  const STATUSES: ReadonlyArray<{ name: string; entry: Record<string, unknown>; status: string }> =
    [
      {
        name: "a stopped tool with both layers holding it",
        entry: ROW_BOTH_LAYERS,
        status: "Stopped",
      },
      { name: "a running tool with neither layer set", entry: ROW_RUNNING, status: "Running" },
      {
        name: "stopped true with both states null and unreadable false",
        entry: row({ tool: "approve_request", stopped: true }),
        status: "Stopped",
      },
      {
        name: "stopped false with the platform layer present",
        entry: row({ tool: "reject_request", global_state: GLOBAL_STATE }),
        status: "Running",
      },
    ];

  it.each(STATUSES)("reads $status for $name", async ({ entry, status }) => {
    // §1's fourth criterion and decision C: the cell is read off `stopped` and
    // NEVER recomputed from the two layer states. Rows 3 and 4 are the kill,
    // and they are arranged so ONLY that recomputation flips them — row 3 is
    // stopped with nothing visible to derive it from (a board computing
    // `global_state !== null || tenant_state !== null` renders "Running" over a
    // tool whose every call is denied, §5's first risk), and row 4 is running
    // with a layer present (the same board renders "Stopped" and sends an
    // operator to clear a switch that is holding nothing down). Rows 1 and 2
    // agree with both implementations on purpose: they pin the two words
    // themselves, so a cell rendering `true`/`false`, or "Disabled", is red
    // without either derivation being involved. `toBe` rather than
    // `toHaveTextContent`, so "Not stopped" cannot satisfy "Stopped".
    boardAnswer = () => board([entry]);
    renderBoard();

    const rows = await switchRows();
    expect(cellText(at(rows, 0), STATUS_CELL)).toBe(status);
  });
});

describe("KillSwitchBoard — the two layer cells", () => {
  it("renders each layer's reason, actor and formatted instant in its own cell", async () => {
    // §1's fifth criterion, with `within(row)` and a POSITION, because
    // `role="cell"` has no name of its own. The two layers carry different
    // reasons, different actors and different instants, so this kills the swap:
    // a board rendering the platform switch under `This tenant` would tell an
    // admin their own tenant stopped a tool the platform stopped, and clearing
    // the tenant switch would change nothing. Each cell is asserted NOT to
    // carry the other layer's reason, which is the half a containment
    // assertion on the row would miss. The instant is asserted as
    // `formatSwitchInstant`'s output and the raw wire string as ABSENT, so a
    // cell printing `2026-09-12T08:00:00+00:00` at an operator is red: that is
    // the wiring decision B exists for, and the format itself is pinned by
    // `killswitch-format.test.ts`.
    boardAnswer = () => board([ROW_BOTH_LAYERS]);
    renderBoard();

    const target = await rowFor("request_access");
    const globalCell = cellText(target, GLOBAL_CELL);
    expect(globalCell).toContain("Fake platform-wide stop.");
    expect(globalCell).toContain("fake-admin-one");
    expect(globalCell).toContain(GLOBAL_RENDERED);
    expect(globalCell).not.toContain("2026-09-12T08:00:00+00:00");
    expect(globalCell).not.toContain("Fake tenant-only stop.");

    const tenantCell = cellText(target, TENANT_CELL);
    expect(tenantCell).toContain("Fake tenant-only stop.");
    expect(tenantCell).toContain("fake-admin-two");
    expect(tenantCell).toContain(TENANT_RENDERED);
    expect(tenantCell).not.toContain("2026-09-11T17:30:45+00:00");
    expect(tenantCell).not.toContain("Fake platform-wide stop.");
  });

  const EMPTY_LAYERS: ReadonlyArray<{
    name: string;
    entry: Record<string, unknown>;
    empty: number;
    held: number;
  }> = [
    {
      name: "no platform switch beside a tenant one",
      entry: row({ tool: "request_access", stopped: true, tenant_state: TENANT_STATE }),
      empty: GLOBAL_CELL,
      held: TENANT_CELL,
    },
    {
      name: "no tenant switch beside a platform one",
      entry: row({ tool: "request_access", stopped: true, global_state: GLOBAL_STATE }),
      empty: TENANT_CELL,
      held: GLOBAL_CELL,
    },
  ];

  it.each(EMPTY_LAYERS)("renders No switch for $name", async ({ entry, empty, held }) => {
    // §1's fifth criterion, the null half, asserted PER CELL. Kills a board
    // rendering a null layer as an empty cell, where an operator cannot tell
    // "no switch here" from a cell that failed to draw — on the one screen
    // whose job is saying which layer holds a tool down. The held cell is
    // asserted not to read `No switch` in the same case, which kills the
    // opposite fault: a board printing the placeholder in every layer cell
    // regardless, which the empty-cell assertion alone would call correct.
    boardAnswer = () => board([entry]);
    renderBoard();

    const target = await rowFor("request_access");
    expect(cellText(target, empty)).toBe(NO_SWITCH);
    expect(cellText(target, held)).not.toContain(NO_SWITCH);
  });
});

describe("KillSwitchBoard — the two row markers", () => {
  it("marks a protected row and leaves an unprotected one unmarked", async () => {
    // §1's sixth criterion, both directions in one case because the presence
    // assertion alone is green for a board printing `Protected` on every row.
    // Kills a board that drops the marker — 24db's disabled toggle is meant to
    // read off this row rather than off a second browser-side copy of
    // `PROTECTED_TOOLS`, and a second copy is exactly what an implementer
    // writes when nothing on the row says so. `set_kill_switch` is the tool the
    // flag is true for (it is the only way back from a switch), and the marker
    // belongs in the `Tool` cell so it reads as a property of that tool.
    boardAnswer = () => board([row({ tool: "set_kill_switch", protected: true }), ROW_RUNNING]);
    renderBoard();

    expect(cellText(await rowFor("set_kill_switch"), TOOL_CELL)).toContain(PROTECTED_TEXT);
    const running = await rowFor("grant_access");
    expect(running.textContent ?? "").not.toContain(PROTECTED_TEXT);
  });

  it("renders an unreadable row as stopped, marked, with neither layer shown", async () => {
    // §1's seventh criterion and §5's second risk, all three facts in one case
    // because each alone is survivable. Kills a board that renders a row whose
    // stored value will not parse as a clean running line: every call to that
    // tool is denied and this board is the ONLY surface a bad key can be seen
    // from, so "Running" there is the one answer an operator cannot act on.
    // The two `No switch` cells are asserted because the tool nulls both states
    // on such a row on purpose (24ca decision D) — a board inventing a
    // placeholder state, or leaving the cells blank, hides which layer to look
    // at. The marker separates this row from a legitimately stopped one, which
    // the `Stopped` assertion alone cannot.
    boardAnswer = () =>
      board([row({ tool: "list_kill_switches", stopped: true, unreadable: true }), ROW_RUNNING]);
    renderBoard();

    const target = await rowFor("list_kill_switches");
    expect(cellText(target, STATUS_CELL)).toBe("Stopped");
    expect(within(target).getByText(UNREADABLE_TEXT)).toBeInTheDocument();
    expect(cellText(target, GLOBAL_CELL)).toBe(NO_SWITCH);
    expect(cellText(target, TENANT_CELL)).toBe(NO_SWITCH);
    const clean = await rowFor("grant_access");
    expect(clean.textContent ?? "").not.toContain(UNREADABLE_TEXT);
  });
});

describe("KillSwitchBoard — nothing to show, and reads that fail", () => {
  it("renders the named empty state with no table and no alert", async () => {
    // §1's eighth criterion, the successful half. Kills a board rendering an
    // empty grid under its headers, where "no tools registered" looks exactly
    // like a read that broke. The absent alert is the mutual exclusion: a page
    // showing both says two contradictory things about one answer, and an
    // operator cannot tell whether to trust the emptiness.
    boardAnswer = () => board([]);
    renderBoard();

    expect(await screen.findByText(EMPTY_STATE)).toBeInTheDocument();
    expect(missingTable()).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  const FAILURES: ReadonlyArray<{ name: string; answer: BoardAnswer }> = [
    {
      name: "a payload the parser refuses",
      answer: () =>
        jsonResponse(
          {
            switches: [
              {
                tool: "request_access",
                protected: false,
                global_state: null,
                tenant_state: null,
                unreadable: false,
              },
            ],
          },
          200,
        ),
    },
    {
      name: "the proxy's own 502",
      answer: () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
    {
      name: "a 500 carrying no JSON at all",
      answer: () =>
        new Response("<html>bad gateway</html>", {
          status: 500,
          headers: { "content-type": "text/html" },
        }),
    },
  ];

  it.each(FAILURES)(
    "$name renders the read message, no table and no empty state",
    async ({ answer }) => {
      // §1's second unhappy path and decision E, from three directions, all
      // killing one implementation: a fetcher that answers an EMPTY BOARD when
      // the read fails. It would print "No tools are registered." over a
      // platform whose switches nobody could read — the one sentence on this
      // page an operator acts on by concluding the platform is clean — because
      // a proxy was down.
      //
      // Row 1 is a row missing `stopped` alone, so it also kills a page that
      // CASTS the body instead of running `safeParse`: the row would draw with
      // an undefined status, which renders a stopped tool as running. Row 2
      // kills one reading the body without checking the status, a 502 envelope
      // being valid JSON, and it is the decision E discriminator —
      // `UPSTREAM_UNAVAILABLE` is not one of the design's eight codes, so it is
      // a read failure and not a typed refusal, and a board answering
      // `failureMessage(code)` here shows "Something went wrong. Nothing was
      // changed.", which is red against this constant. Row 3 kills one calling
      // `response.json()` unguarded, where the parse error escapes as an
      // unhandled rejection instead of a sentence.
      boardAnswer = answer;
      renderBoard();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(KILL_SWITCH_READ_MESSAGE);
      expect(missingTable()).toBeNull();
      expect(screen.queryByText(EMPTY_STATE)).toBeNull();
    },
    10_000,
  );
});

describe("KillSwitchBoard — a refused read", () => {
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
      // §1's first unhappy path and decision E, one case per reachable code.
      // Kills a board rendering the CODE at the operator ("TOOL_DISABLED" means
      // nothing to the person reading it), and kills one answering every
      // failure with `KILL_SWITCH_READ_MESSAGE` — a caller who is not a
      // `platform_admin` would be told to "try again in a moment" for something
      // no amount of retrying fixes, and `list_kill_switches` held down by a
      // switch of its own would read as a broken proxy rather than as the
      // deliberate stop it is. Both sentences are READ from `FAILURE_MESSAGES`,
      // so a board inventing its own wording is red without widening 22b's
      // pinned map, and this surface takes NO override sentence (decision E):
      // both shared sentences are true of this read.
      boardAnswer = () => jsonResponse({ code }, status);
      renderBoard();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(sentence);
      expect(screen.queryByText(KILL_SWITCH_READ_MESSAGE)).toBeNull();
      expect(screen.queryByText(EMPTY_STATE)).toBeNull();
      expect(missingTable()).toBeNull();
    },
    10_000,
  );
});

describe("KillSwitchBoard — an expired session", () => {
  it("follows the proxy's redirect to /login instead of rendering a board", async () => {
    // §1's last unhappy path, and the reason it is `response.redirected` and
    // never a status (22b decision E, a project rule): the browser has
    // ALREADY followed the proxy's redirect, so what arrives is a 200 carrying
    // the login page. Kills a fetcher checking `response.ok` or
    // `response.status === 401` — both see a successful response, the parser
    // then refuses the HTML, and the operator sits on `/admin/killswitch`
    // reading "the kill switches could not be read" for a session that merely
    // expired. The absent EMPTY STATE is the worse half and is why this case
    // asserts all three: an expired session rendered as "No tools are
    // registered." is a platform reporting itself clean to somebody who is not
    // even signed in.
    boardAnswer = redirectedResponse;
    renderBoard();

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(missingTable()).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(EMPTY_STATE)).toBeNull();
  });
});

describe("KillSwitchBoard — two failed reads the existing rows do not cover (plan 24ib §6)", () => {
  it("renders the read message for a 500 carrying no body at all", async () => {
    // An earlier change §5's second risk, named for this board: `readWire` reads the
    // body of a non-ok response to look for a typed code, and the existing
    // `a 500 carrying no JSON at all` row still hands it HTML BYTES to fail on.
    // This one is `new Response(null, ...)` — nothing to read at all. Kills a
    // fetcher whose error path calls `response.json()` unguarded: the parse
    // error escapes as an unhandled rejection instead of a sentence, and the
    // operator is left on a kill switch screen that says nothing whatever
    // while the proxy behind it is down. The empty state is asserted absent
    // because the other wrong answer — an outage rendered as "No tools are
    // registered." — is the one sentence here an operator acts on by
    // concluding the platform is clean.
    boardAnswer = () => new Response(null, { status: 500 });
    renderBoard();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(KILL_SWITCH_READ_MESSAGE);
    expect(missingTable()).toBeNull();
    expect(screen.queryByText(EMPTY_STATE)).toBeNull();
  }, 10_000);

  it("renders the read message for a 200 whose body is not JSON at all", async () => {
    // An earlier change decision E on this board: today `readJson` guards the parse and
    // `killSwitchBoardSchema` refuses the `undefined`; through `readWire` the
    // same response takes the same route to the same sentence, and the case is
    // what turns that equivalence from an argument into an assertion. Kills a
    // migration that lets a 200 carrying an HTML error page through the ok
    // branch without a parse — the board would paint from an unvalidated body,
    // and a row whose `stopped` nobody checked renders a stopped tool as
    // running, which is §5's first risk arriving by a different door.
    boardAnswer = () =>
      new Response("<html>not json</html>", {
        status: 200,
        headers: { "content-type": "text/html" },
      });
    renderBoard();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(KILL_SWITCH_READ_MESSAGE);
    expect(missingTable()).toBeNull();
    expect(screen.queryByText(EMPTY_STATE)).toBeNull();
  }, 10_000);
});
