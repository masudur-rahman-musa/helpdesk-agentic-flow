import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RulesBoard } from "@/components/admin/rules-board";
import { QueryProvider } from "@/components/query-provider";
import { FAILURE_MESSAGES } from "@/lib/chat-errors";
import { RULES_READ_MESSAGE, UNRESOLVED_ROLE_TEXT } from "@/lib/rules-labels";

/**
 * An earlier change §6, `rules-board.test.tsx`: the read-only `/admin/rules` board — one
 * row per separation of duties rule, both sides side by side — against a
 * stubbed `fetch`. jsdom, no agent-api, no backing service. (No basename
 * collision in the tree, a project rule: the neighbours are
 * `rules-schema.test.ts` and `rules-proxy-routes.test.ts`.)
 *
 * The unit under test is `RulesBoard` from
 * `web/components/admin/rules-board.tsx`, taking NO props and doing its own
 * read through TanStack Query (§2, decision A), so `app/admin/rules/page.tsx`
 * stays a thin `AppShell` wrapper no case here renders. Everything on this
 * surface is a READ: v1 is read-only (the design), so no case clicks anything and
 * no case posts anything.
 *
 * The accessible names are the plan's own (§6): the region `Rules`, the table
 * `Separation of duties rules`, and the four column headers `Role A`, `Role B`,
 * `Rationale`, `Policy`. The implementation owes the `<section>` and the
 * `<table>` an `aria-label` each, because neither role takes its name from its
 * contents.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * they belong in 24f's Deviations):
 * - "no query string and no body" is asserted as: exactly one call, whose
 *   pathname is `/api/admin/rules`, whose `search` is empty, whose method is
 *   `GET` (absent counts, `fetch`'s own default) and whose `body` is
 *   `undefined`. The relative-versus-absolute form of the URL is NOT fixed.
 * - a row's four cells are read BY POSITION in the header order above:
 *   `role="cell"` carries no name of its own, so `within(row)` alone cannot
 *   tell the `Role A` cell from the `Role B` one — which is the swap several
 *   of these cases exist to catch.
 * - the rows are identified by their `Policy` cell, which is the one value
 *   every fixture here gives a distinct string to.
 * - an expired session is `response.redirected` with `url` on the INSTANCE and
 *   `location` stubbed through `vi.stubGlobal`.
 *
 * Every fixture parses under `web/lib/rules-schema.ts` as 24eb built it: the
 * seven `SodRuleRow` fields inside `{ rules: [...] }`, with `role_a` and
 * `role_b` the two nullable ones. That parser is pinned by
 * `tests/rules-schema.test.ts` and only CONSUMED here. All names are obviously
 * fake.
 */

const ORIGIN = "http://localhost:3000";
const BOARD_PATH = "/api/admin/rules";
const LOGIN_URL = "http://localhost:3000/login";
const EMPTY_STATE = "No separation of duties rules are configured.";

/** The four columns, in the order §1 fixes them. */
const ROLE_A_CELL = 0;
const ROLE_B_CELL = 1;
const RATIONALE_CELL = 2;
const POLICY_CELL = 3;

/**
 * One side of a rule, resolved: the five fields the tool sends, of which the
 * board renders three. The two sides differ in every field, so a board
 * rendering `role_a` under `Role B` is red rather than accidentally right, and
 * the slug is lower case where the name is not, so a `toContain` for the slug
 * cannot be satisfied by the name.
 */
const ROLE_A_BLOCK: Record<string, unknown> = {
  role_id: "aaaaaaaa-0000-4000-8000-000000000001",
  slug: "fake-approver",
  name: "Fake Approver",
  system_slug: "fake-ledger",
  system_name: "Fake Ledger",
};

const ROLE_B_BLOCK: Record<string, unknown> = {
  role_id: "bbbbbbbb-0000-4000-8000-000000000002",
  slug: "fake-poster",
  name: "Fake Poster",
  system_slug: "fake-books",
  system_name: "Fake Books",
};

const ROLE_A_ID = "aaaaaaaa-0000-4000-8000-000000000001";
const ROLE_B_ID = "bbbbbbbb-0000-4000-8000-000000000002";

function rule(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    rule_id: "cccccccc-0000-4000-8000-000000000009",
    role_a_id: ROLE_A_ID,
    role_a: ROLE_A_BLOCK,
    role_b_id: ROLE_B_ID,
    role_b: ROLE_B_BLOCK,
    rationale: "Fake rationale for a fake pair of roles.",
    policy_ref: "SOD-A",
    ...overrides,
  };
}

/** Both sides resolved: the row every resolved-cell assertion reads. */
const RESOLVED_RULE = rule({});

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

type BoardAnswer = () => Response | Promise<Response>;

let boardAnswer: BoardAnswer = () => board([RESOLVED_RULE]);

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The board in `sodRulesBoardSchema`'s envelope. */
function board(rules: Record<string, unknown>[]): Response {
  return jsonResponse({ rules }, 200);
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
      <RulesBoard />
    </QueryProvider>,
  );
}

async function region(): Promise<HTMLElement> {
  return await screen.findByRole("region", { name: "Rules" });
}

async function table(): Promise<HTMLElement> {
  return await within(await region()).findByRole("table", { name: "Separation of duties rules" });
}

function missingTable(): HTMLElement | null {
  return screen.queryByRole("table", { name: "Separation of duties rules" });
}

/** The table's DATA rows: the ones holding cells rather than column headers. */
async function ruleRows(): Promise<HTMLElement[]> {
  return within(await table())
    .getAllByRole("row")
    .filter((candidate) => within(candidate).queryAllByRole("cell").length > 0);
}

/** One row's cell at a column index, with its text collapsed. */
function cellText(rowElement: HTMLElement, column: number): string {
  const cells = within(rowElement).getAllByRole("cell");
  return (at(cells, column).textContent ?? "").trim();
}

/** The rendered `Policy` cell of every data row, in the order they are painted. */
async function renderedPolicyRefs(): Promise<string[]> {
  return (await ruleRows()).map((rowElement) => cellText(rowElement, POLICY_CELL));
}

/** The one data row whose `Policy` cell names this ref. */
async function rowFor(policyRef: string): Promise<HTMLElement> {
  const rows = await ruleRows();
  const match = rows.filter((rowElement) => cellText(rowElement, POLICY_CELL).includes(policyRef));
  if (match.length !== 1) {
    throw new Error(`expected exactly one row for ${policyRef}, found ${match.length}`);
  }
  return at(match, 0);
}

beforeEach(() => {
  boardAnswer = () => board([RESOLVED_RULE]);
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
    href: `${ORIGIN}/admin/rules`,
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RulesBoard — the read it makes", () => {
  it("reads the proxy once on paint, as a GET with no query and no body", async () => {
    // §1's first criterion and decision A, as the call's whole argument list.
    // Kills a board that sends the tenant, the principal or a policy filter
    // along (`?tenant=...`): which rules this caller sees is the token's
    // tenant, decided behind the MCP boundary, and a page that names one is
    // a project rule written into the browser. Kills a POST — nothing on this
    // surface may write, v1 being read-only. Kills a board that reads on a
    // click instead of on paint (no call at all by the time the table exists),
    // and one firing twice per paint, which doubles the tool's work for a
    // screen nobody interacted with.
    renderBoard();

    await ruleRows();
    expect(boardCalls()).toHaveLength(1);
    const [input, init] = at(boardCalls(), 0);
    const url = new URL(urlOf(input), ORIGIN);
    expect(url.pathname).toBe(BOARD_PATH);
    expect(url.search).toBe("");
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.body).toBeUndefined();
  });
});

describe("RulesBoard — the shape of the board", () => {
  it("renders the four column headers in order", async () => {
    // §1's third criterion as an exact sequence, and the guard every cell case
    // below leans on: those are positional, so a column inserted anywhere must
    // be red HERE rather than silently making four cell cases read the wrong
    // column. Kills a board that adds a `Rule` column of uuids (decision B:
    // no control on this page takes a rule id, and 36 characters in the first
    // column costs the rationale its width on every row), and kills one that
    // renames or reorders the two sides so `Role A` and `Role B` no longer
    // match the ids the unresolved cases quote.
    renderBoard();

    const headers = within(await table())
      .getAllByRole("columnheader")
      .map((header) => (header.textContent ?? "").trim());
    expect(headers).toEqual(["Role A", "Role B", "Rationale", "Policy"]);
  });

  const ORDERS: ReadonlyArray<{ name: string; refs: readonly string[] }> = [
    { name: "reverse alphabetical", refs: ["SOD-C", "SOD-B", "SOD-A"] },
    { name: "neither sort order", refs: ["SOD-B", "SOD-A", "SOD-C"] },
  ];

  it.each(ORDERS)("renders a payload in $name order exactly as it arrived", async ({ refs }) => {
    // §1's second criterion, with two payload orders because ONE proves
    // nothing: a board sorting ascending would pass a payload that happened to
    // arrive sorted. Row 1 kills an ascending sort, row 2 kills a descending
    // one and any "group by policy ref" arrangement — its three refs are in
    // neither alphabetical direction. The order on the wire is the tool's
    // total order (`policy_ref` ascending then `rule_id` ascending, 24ea
    // decision C), and this component is not the place that decides what the
    // board contains or in what order: a rule added by a later plan must not be
    // able to land somewhere an admin does not look (§5's first risk).
    boardAnswer = () =>
      board(refs.map((ref, index) => rule({ policy_ref: ref, rule_id: `rule-${index}` })));
    renderBoard();

    await waitFor(async () => {
      expect(await renderedPolicyRefs()).toHaveLength(refs.length);
    });
    expect(await renderedPolicyRefs()).toEqual([...refs]);
  });
});

describe("RulesBoard — what a row shows", () => {
  it("renders a resolved side's name, slug and system name, and neither id nor system slug", async () => {
    // §1's fourth criterion, both halves, and decision B pinned as an ABSENCE
    // rather than assumed. Kills a board rendering only the role NAME: two
    // systems may name a role the same thing, so the slug and the system are
    // what tell an admin WHICH `Fake Approver` the rule is about. Kills one
    // printing `role_id` or `system_slug` into the cell — a uuid beside three
    // words is the column-width cost decision B refuses, and the system slug
    // says nothing the system name has not already said in words. The two
    // sides carry different values in every field, so this also kills the swap:
    // the `Role A` cell is asserted not to carry `Role B`'s words, which a
    // containment assertion on the whole ROW would miss entirely.
    boardAnswer = () => board([RESOLVED_RULE]);
    renderBoard();

    const target = await rowFor("SOD-A");
    const roleA = cellText(target, ROLE_A_CELL);
    expect(roleA).toContain("Fake Approver");
    expect(roleA).toContain("fake-approver");
    expect(roleA).toContain("Fake Ledger");
    expect(roleA).not.toContain(ROLE_A_ID);
    expect(roleA).not.toContain("fake-ledger");
    expect(roleA).not.toContain("Fake Poster");

    const roleB = cellText(target, ROLE_B_CELL);
    expect(roleB).toContain("Fake Poster");
    expect(roleB).toContain("fake-poster");
    expect(roleB).toContain("Fake Books");
    expect(roleB).not.toContain(ROLE_B_ID);
    expect(roleB).not.toContain("fake-books");
    expect(roleB).not.toContain("Fake Approver");
  });

  it("renders an unresolved role_a as its id and the marker, with role_b untouched", async () => {
    // §1's fifth criterion and §5's second risk. The parser's `.nullable()`
    // makes this row expressible and it is the one row an admin came to this
    // page for. Kills a board written as `row.role_a?.name`, which paints an
    // empty cell and loses the fact that a side could not be resolved
    // (decision F: a blank cell is indistinguishable from one that failed to
    // draw). Kills one rendering `Unknown` alone, which leaves no handle to go
    // looking with — the id is what a support request or a `psql` query needs —
    // and kills one that drops the whole ROW because half of it would not
    // resolve. The `Role B` half of the same row is asserted normal, which
    // kills a board that gives up on a row as soon as either side is null.
    boardAnswer = () => board([rule({ role_a: null })]);
    renderBoard();

    const target = await rowFor("SOD-A");
    const roleA = cellText(target, ROLE_A_CELL);
    expect(roleA).toContain(ROLE_A_ID);
    expect(roleA).toContain(UNRESOLVED_ROLE_TEXT);
    expect(UNRESOLVED_ROLE_TEXT).toBe("Role not found");

    const roleB = cellText(target, ROLE_B_CELL);
    expect(roleB).toContain("Fake Poster");
    expect(roleB).toContain("fake-poster");
    expect(roleB).toContain("Fake Books");
    expect(roleB).not.toContain(UNRESOLVED_ROLE_TEXT);
  });

  it("renders a rule whose two sides are both unresolved with its rationale and policy", async () => {
    // §1's sixth criterion. Kills a board that renders the marker for the first
    // null side only (the shape an `if (role_a === null) ... else ...` written
    // once produces), and kills one that treats a doubly unresolved rule as
    // unrenderable and drops it: a rule nobody can see is a control nobody
    // reviews, while the SoD engine is still enforcing it. The rationale and
    // the policy ref are asserted in the same case because they are the only
    // two facts left on such a row — they are what an admin quotes when
    // reporting it, decision B having left no id column.
    boardAnswer = () =>
      board([
        rule({
          role_a: null,
          role_b: null,
          rationale: "Fake rationale for the unresolved pair.",
          policy_ref: "SOD-Z",
        }),
      ]);
    renderBoard();

    const target = await rowFor("SOD-Z");
    const roleA = cellText(target, ROLE_A_CELL);
    expect(roleA).toContain(ROLE_A_ID);
    expect(roleA).toContain(UNRESOLVED_ROLE_TEXT);

    const roleB = cellText(target, ROLE_B_CELL);
    expect(roleB).toContain(ROLE_B_ID);
    expect(roleB).toContain(UNRESOLVED_ROLE_TEXT);

    expect(cellText(target, RATIONALE_CELL)).toContain("Fake rationale for the unresolved pair.");
    expect(cellText(target, POLICY_CELL)).toContain("SOD-Z");
  });
});

describe("RulesBoard — nothing to show, and reads that fail", () => {
  it("renders the named empty state with no table and no alert", async () => {
    // §1's seventh criterion, the successful half. Kills a board rendering an
    // empty grid under its headers, where "no rules configured" looks exactly
    // like a read that broke. The absent alert is the mutual exclusion: a page
    // showing both says two contradictory things about one answer, and an
    // admin cannot tell whether to trust the emptiness. The sentence is
    // asserted verbatim because it is the one an admin acts on by concluding
    // the tenant has no SoD control at all.
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
            rules: [
              {
                rule_id: "cccccccc-0000-4000-8000-000000000009",
                role_a_id: ROLE_A_ID,
                role_a: ROLE_A_BLOCK,
                role_b_id: ROLE_B_ID,
                role_b: ROLE_B_BLOCK,
                policy_ref: "SOD-A",
              },
            ],
          },
          200,
        ),
    },
    {
      name: "a 200 carrying no JSON at all",
      answer: () =>
        new Response("<html>not json</html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
    },
    {
      name: "the proxy's own 502",
      answer: () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ];

  it.each(FAILURES)(
    "$name renders the read message, no table and no empty state",
    async ({ answer }) => {
      // §1's second unhappy path and a project rule's discriminator, from
      // three directions, all killing one implementation: a board that answers
      // an EMPTY LIST when the read fails. It would print "No separation of
      // duties rules are configured." over a tenant whose rules nobody could
      // read — the one sentence on this page an admin acts on by concluding
      // there is no SoD control at all — because a proxy was down.
      //
      // Row 1 is a rule missing `rationale` alone, so it also kills a page that
      // CASTS the body instead of running `safeParse`: the row would draw with
      // an undefined rationale, a rule on screen with no reason for existing.
      // Row 2 kills one calling `response.json()` unguarded on a 200, where the
      // parse error escapes as an unhandled rejection instead of a sentence.
      // Row 3 kills one reading the body without checking the status, a 502
      // envelope being valid JSON, and it is the discriminator in one case:
      // `UPSTREAM_UNAVAILABLE` is not one of the design's eight codes, so it is
      // a failed read and not a typed refusal, and a board answering
      // `failureMessage(code)` here shows "Something went wrong. Nothing was
      // changed.", which is red against this constant.
      boardAnswer = answer;
      renderBoard();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(RULES_READ_MESSAGE);
      // Decision E: this page's two strings have no test file of their own, so
      // the sentence is pinned VERBATIM where it is rendered. Kills a constant
      // reworded to `FAILURE_MESSAGES.default`, which says only that something
      // went wrong and nothing about WHICH thing could not be read, on a page
      // whose alternative reading is "this tenant has no SoD control".
      expect(RULES_READ_MESSAGE).toBe(
        "The separation of duties rules could not be read. Try again in a moment.",
      );
      expect(missingTable()).toBeNull();
      expect(screen.queryByText(EMPTY_STATE)).toBeNull();
    },
    10_000,
  );
});

describe("RulesBoard — a refused read", () => {
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
      // §1's first unhappy path, one case per reachable code. Kills a board
      // rendering the CODE at the admin ("TOOL_DISABLED" means nothing to the
      // person reading it), and kills one answering every failure with
      // `RULES_READ_MESSAGE` — a caller who is neither `it_admin` nor
      // `platform_admin` would be told to "try again in a moment" for something
      // no amount of retrying fixes, and `list_sod_rules` held down by a switch
      // would read as a broken proxy rather than as the deliberate stop it is.
      // Both sentences are READ from `FAILURE_MESSAGES` rather than retyped, so
      // a board inventing its own wording is red without widening 22b's pinned
      // map. The absent read message is what separates this path from the one
      // above it.
      boardAnswer = () => jsonResponse({ code }, status);
      renderBoard();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(sentence);
      expect(screen.queryByText(RULES_READ_MESSAGE)).toBeNull();
      expect(screen.queryByText(EMPTY_STATE)).toBeNull();
      expect(missingTable()).toBeNull();
    },
    10_000,
  );
});

describe("RulesBoard — an expired session", () => {
  it("follows the proxy's redirect to /login instead of rendering a board", async () => {
    // §1's last unhappy path, and the reason it is `response.redirected` and
    // never a status (22b decision E, a project rule): the browser has
    // ALREADY followed the proxy's redirect, so what arrives is a 200 carrying
    // the login page. Kills a fetcher checking `response.ok` or
    // `response.status === 401` — both see a successful response, the parser
    // then refuses the HTML, and the admin sits on `/admin/rules` reading "the
    // rules could not be read" for a session that merely expired. The absent
    // EMPTY STATE is the worse half and is why this case asserts all three: an
    // expired session rendered as "No separation of duties rules are
    // configured." tells somebody who is not even signed in that the tenant has
    // no separation of duties at all.
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
