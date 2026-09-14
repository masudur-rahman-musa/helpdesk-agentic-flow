import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/query-provider";
import { RequestList } from "@/components/requests/request-list";
import { FAILURE_MESSAGES } from "@/lib/chat-errors";
import { reasonSentence } from "@/lib/decision-labels";

/**
 * An earlier change §6, `requests-page.test.tsx`: the `/requests` list against a stubbed
 * `fetch`. jsdom, no agent-api, no backing service. (No basename collision in
 * the tree — a project rule.)
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23b's Deviations):
 * - the unit under test is `RequestList` from
 *   `web/components/requests/request-list.tsx`, taking **no props** and doing
 *   its own read of `/api/me/requests` through TanStack Query (decision A) —
 *   the same division `ChatPanel` has, so `app/requests/page.tsx` stays a thin
 *   wrapper a test never has to await.
 * - the accessible names: region `My requests`, the row list `Requests`, and
 *   INSIDE a row the three nested lists `Reasons`, `Approvers` and `Sources`.
 *   §6 names only the outer two and `Approvers`; the other two are needed for
 *   "one listitem per reason" and "one listitem per citation" to be countable
 *   at all, since a row holding three lists cannot be counted by `listitem`.
 * - a row's listitems are its DIRECT children of the `Requests` list
 *   (`rowsOf`), because `getAllByRole("listitem")` inside a list also returns
 *   every nested list's items.
 * - the wording of the two expiry lines is fixed and deliberately disjoint:
 *   the request's own ask reads `30 days requested`, and a granted row's
 *   `expires_at` reads `Expires 2026-12-01` (the ISO prefix, the same
 *   unambiguous form the decision card already prints — a locale date is a
 *   different date in half the world). The disjointness is what makes "a
 *   pending row shows no expiry line at all" assertable as the absence of
 *   `Expires`.
 * - the empty state is the literal sentence of §6, and a failed read is a
 *   `role="alert"` element.
 *
 * The wire shape is 23a's as BUILT: nineteen fields per row inside
 * `{ requests: [...] }` (`mcp_server.tools.access`, parsed by
 * `web/lib/access-schema.ts`), a citation being
 * `{document_slug, title, version, heading_path, chunk_id}` with `heading_path`
 * a STRING (`tools/access.py::Citation`), and a failed proxy answering
 * `502 {"code": "UPSTREAM_UNAVAILABLE"}` (`proxyAgentApi`).
 */

const EMPTY_REQUESTS = "You have not asked for any access yet.";
const RULE_ID = "22222222-2222-5222-8222-222222222222";
const LOGIN_URL = "http://localhost:3000/login";

/** A pending request, nothing granted: `grant_id` and `expires_at` are null. */
const PENDING_ROW: Record<string, unknown> = {
  request_id: "66666666-6666-5666-8666-666666666666",
  target_user_id: "11111111-1111-5111-8111-111111111111",
  role_id: "77777777-7777-5777-8777-777777777777",
  justification: "Obviously fake justification.",
  requested_expiry_days: 30,
  risk_class: "elevated",
  decision: "approval",
  status: "pending",
  created_at: "2026-06-02T10:15:00Z",
  role_slug: "payroll-read",
  role_name: "Fake Payroll Read",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  target_display_name: "Fake Person One",
  decision_reasons: ["NOT_IN_TEMPLATE", `SOD_CONFLICT:${RULE_ID}`],
  citations: [],
  approvals: [
    { slot: "department_manager", user_id: "33333333-3333-5333-8333-333333333333" },
    { slot: "it_admin", user_id: null },
  ],
  grant_id: null,
  expires_at: null,
};

/** An executed request: it HAS a grant, so the grant's expiry is renderable. */
const EXECUTED_ROW: Record<string, unknown> = {
  ...PENDING_ROW,
  request_id: "88888888-8888-5888-8888-888888888888",
  requested_expiry_days: null,
  risk_class: "low",
  decision: "auto",
  status: "executed",
  role_slug: "warehouse-read",
  role_name: "Fake Warehouse Read",
  system_slug: "warehouse",
  system_name: "Fake Warehouse",
  decision_reasons: ["IN_TEMPLATE"],
  citations: [
    {
      document_slug: "fake-access-policy",
      title: "Fake Access Policy",
      version: "v3",
      heading_path: "Fake Section › Fake Subsection",
      chunk_id: "99999999-9999-5999-8999-999999999999",
    },
    {
      document_slug: "fake-joiner-policy",
      title: "Fake Joiner Policy",
      version: "v1",
      heading_path: "Fake Section",
      chunk_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
    },
  ],
  approvals: [],
  grant_id: "55555555-5555-5555-8555-555555555555",
  expires_at: "2026-12-01T09:30:00Z",
};

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

let answer: () => Response = () => jsonResponse({ requests: [] }, 200);

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/**
 * The response a browser is handed after FOLLOWING the proxy's redirect to
 * `/login`: a 200 carrying the login page, with `redirected` true and `url`
 * pointing at where it ended up. `Response` has no constructor for either, so
 * both are defined on the instance.
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

function renderList(): void {
  render(
    <QueryProvider>
      <RequestList />
    </QueryProvider>,
  );
}

/** The list's own rows: its DIRECT listitem children, never a nested list's. */
function rowsOf(list: HTMLElement): HTMLElement[] {
  return within(list)
    .getAllByRole("listitem")
    .filter((item) => item.parentElement === list);
}

async function requestRows(): Promise<HTMLElement[]> {
  const region = await screen.findByRole("region", { name: "My requests" });
  return rowsOf(await within(region).findByRole("list", { name: "Requests" }));
}

/** One element of a query's result, as a value the type checker can use. */
function at(elements: HTMLElement[], index: number): HTMLElement {
  const element = elements[index];
  if (element === undefined) {
    throw new Error(`expected an element at index ${index}, found ${elements.length}`);
  }
  return element;
}

function itemsOf(row: HTMLElement, name: string): HTMLElement[] {
  return within(within(row).getByRole("list", { name })).getAllByRole("listitem");
}

beforeEach(() => {
  answer = () => jsonResponse({ requests: [] }, 200);
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : "";
    if (url.includes("/api/me/requests")) {
      return answer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", { ...window.location, href: "http://localhost:3000/requests", assign });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RequestList — the rows", () => {
  it("renders one row per request, with its role, system, target, decision, risk, status, reasons and approvers", async () => {
    // Catches a list that renders only the first request (`requests[0]` rather
    // than a map), which on a real account hides everything but the oldest ask.
    // Within the row: the three label assertions catch a page printing the WIRE
    // values at the user (`approval`, `elevated`, `pending` are the engine's
    // vocabulary, not a person's); the reason assertions catch a row printing
    // the raw codes, and the uuid assertion catches the SoD rule id being
    // interpolated back in, which reads as nothing to the person asking; the
    // approver count plus the two slot labels catch a row that renders the
    // chain as a single joined string, so a two-seat chain cannot be told from
    // one seat named oddly. The empty-state assertion is what stops an
    // implementation showing both at once.
    answer = () => jsonResponse({ requests: [PENDING_ROW, EXECUTED_ROW] }, 200);
    renderList();

    const rows = await requestRows();
    expect(rows).toHaveLength(2);

    const row = at(rows, 0);
    expect(row).toHaveTextContent("Fake Payroll Read");
    expect(row).toHaveTextContent("Fake Payroll");
    expect(row).toHaveTextContent("Fake Person One");
    expect(row).toHaveTextContent("Needs approval");
    expect(row).toHaveTextContent("Elevated risk");
    expect(row).toHaveTextContent("Pending approval");
    expect(row).toHaveTextContent("30 days requested");

    const reasons = itemsOf(row, "Reasons");
    expect(reasons).toHaveLength(2);
    expect(reasons[0]).toHaveTextContent(reasonSentence("NOT_IN_TEMPLATE"));
    expect(reasons[1]).toHaveTextContent(reasonSentence("SOD_CONFLICT"));
    expect(row.textContent ?? "").not.toContain("NOT_IN_TEMPLATE");
    expect(row.textContent ?? "").not.toContain(RULE_ID);

    const approvers = itemsOf(row, "Approvers");
    expect(approvers).toHaveLength(2);
    expect(approvers[0]).toHaveTextContent("Department manager");
    expect(approvers[1]).toHaveTextContent("IT admin");

    expect(screen.queryByText(EMPTY_REQUESTS)).toBeNull();
  });

  it("shows the grant's expiry only on the request that has a grant", async () => {
    // The half a default would get wrong. Catches a row that renders an expiry
    // line for every request — a pending ask would then tell the user their
    // access runs until some date, when nothing has been granted at all — and
    // catches a line built from a null `expires_at`, which prints the literal
    // "null" or "Invalid Date" under the same heading. The ISO assertion
    // catches `toLocaleDateString`, which under jsdom answers "12/1/2026" and
    // in half the world means the first of December read as a different day.
    answer = () => jsonResponse({ requests: [PENDING_ROW, EXECUTED_ROW] }, 200);
    renderList();

    const rows = await requestRows();
    expect(at(rows, 1)).toHaveTextContent("Expires 2026-12-01");
    expect(at(rows, 0)).not.toHaveTextContent(/Expires/);
    expect(at(rows, 0).textContent).not.toContain("null");
    expect(at(rows, 1).textContent).not.toContain("undefined");
  });

  it("renders each citation as its own source, naming the document and its version", async () => {
    // Catches a row that renders the citations as one joined string or as raw
    // JSON (the wire shape is five fields, and `JSON.stringify` on a list of
    // them is what appears when nothing maps it), and catches a row that drops
    // the VERSION: two versions of the same policy then read identically, so a
    // user cannot tell which text their request was decided against. The count
    // is scoped to the `Sources` list inside the row, so the reasons and the
    // approver seats cannot make it pass.
    answer = () => jsonResponse({ requests: [EXECUTED_ROW] }, 200);
    renderList();

    const rows = await requestRows();
    const sources = itemsOf(at(rows, 0), "Sources");

    expect(sources).toHaveLength(2);
    expect(sources[0]).toHaveTextContent("Fake Access Policy");
    expect(sources[0]).toHaveTextContent("v3");
    expect(sources[1]).toHaveTextContent("Fake Joiner Policy");
    expect(sources[1]).toHaveTextContent("v1");
  });
});

describe("RequestList — nothing to show", () => {
  it("renders the named empty state and no list", async () => {
    // Catches a page that renders an empty list under its heading: "you have
    // filed nothing" then looks exactly like a read that returned nothing
    // because it broke, and the user has no way to tell. The absent list is the
    // half a bare sentence added beside an empty `ul` would not satisfy.
    answer = () => jsonResponse({ requests: [] }, 200);
    renderList();

    expect(await screen.findByText(EMPTY_REQUESTS)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Requests" })).toBeNull();
  });
});

describe("RequestList — a read that fails", () => {
  const FAILURES = [
    {
      name: "a payload the parser refuses",
      answer: () =>
        jsonResponse({ requests: [{ request_id: "66666666-6666-5666-8666-666666" }] }, 200),
    },
    {
      name: "the proxy's own 502",
      answer: () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ] as const;

  it.each(FAILURES)(
    "$name renders an alert and no list",
    async (row) => {
      // Both rows catch the same wrong implementation from two directions: a page
      // that answers an empty array when the read fails. It would render §6's
      // empty state — "you have not asked for any access yet" — over a request
      // list the user may well have filed, which is worse than saying nothing.
      // The first row additionally catches a page that CASTS the body instead of
      // running `safeParse` (the row then draws with blank cells and no error at
      // all); the second catches one that reads the body without checking the
      // status, since a 502 envelope is valid JSON.
      answer = row.answer;
      renderList();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toBeInTheDocument();
      expect(screen.queryByRole("list", { name: "Requests" })).toBeNull();
      expect(screen.queryByText(EMPTY_REQUESTS)).toBeNull();
    },
    10_000,
  );
});

describe("RequestList — an expired session", () => {
  it("follows the proxy's redirect to /login instead of rendering rows", async () => {
    // §1's unhappy path, and the reason it is `response.redirected` and never a
    // status (22b decision E): the browser has ALREADY followed the proxy's
    // 307, so what arrives here is a 200 carrying the login page. Catches a
    // fetcher checking `response.ok` or `response.status === 401` — both see a
    // successful response, so the parser refuses the HTML and the user sits on
    // `/requests` reading an error instead of being asked to sign in again.
    answer = redirectedResponse;
    renderList();

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(screen.queryByRole("list", { name: "Requests" })).toBeNull();
  });
});

/**
 * An earlier change §6, added to 23b's file and rewriting none of it (§8): the three
 * cases that hold `fetchRequests` still, after its four private lines become
 * `readWire("/api/me/requests", myRequestsSchema)`.
 *
 * The sentence is not held as a constant anywhere today — it is JSX text inside
 * `request-list.tsx` — so `READ_ALERT` below is it copied byte for byte, and
 * the mismatch with §6's "taken from the constant the file already holds" is
 * reported to 24ia rather than fixed by exporting a constant this plan's §3
 * does not authorise.
 */
describe("RequestList — through the shared wire reader", () => {
  const READ_ALERT = "Your requests could not be read. Try again in a moment.";
  const ORIGIN = "http://localhost:3000";

  /** The single read this page made, as a URL a case can assert field by field. */
  function onlyReadUrl(): URL {
    const urls = fetchStub.mock.calls.map(([input]) =>
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
    );
    const first = urls[0];
    if (urls.length !== 1 || first === undefined) {
      throw new Error(`expected exactly one read, found ${urls.length}`);
    }
    return new URL(first, ORIGIN);
  }

  it("a 403 FORBIDDEN renders this page's own read sentence, not the code's", async () => {
    // Decision B, pinned rather than assumed. Through `readWire` a typed code
    // arrives as a `WireRefusedError` where today every non-ok status arrives as
    // a plain `Error`, so this kills the two migrations that changes: one that
    // dresses the code per page (`failureMessage(error.code)` would put "You are
    // not allowed to do that." here, which no closed plan's §1 asked for and
    // which this file's other cases would stay green for), and one that lets the
    // new class escape the page's error branch entirely — rethrown outside the
    // query function it reaches no `isError` at all and the page sits on its
    // loading state with no alert. The staged body is what makes the class
    // reachable: a 502 `UPSTREAM_UNAVAILABLE` (the rows above) is a plain
    // `Error` through the migrated reader too, so neither of those rows can
    // decide this.
    answer = () => jsonResponse({ code: "FORBIDDEN" }, 403);
    renderList();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(READ_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.FORBIDDEN);
    expect(screen.queryByRole("list", { name: "Requests" })).toBeNull();
    expect(screen.queryByText(EMPTY_REQUESTS)).toBeNull();
  }, 10_000);

  it("reads /api/me/requests with an empty query string", async () => {
    // The path and the query as they are today, asserted as an exact `pathname`
    // and an exact `search` so a migration cannot move the call while it moves
    // the reader. Kills a `readWire` call handed `/api/requests`, `/api/me` or a
    // path with the tenant or the user spliced into it (a project rule: identity is
    // the token's, and a page naming either is the bug this assertion is
    // cheapest at), and kills one that gains a filter or a page size the tool
    // was never asked for — `toContain` on the url would pass for every one of
    // those. Arithmetic: the staged answer is the default
    // empty 200, so reads = 1 on mount and the empty state is what says the read
    // settled before the count is taken.
    answer = () => jsonResponse({ requests: [] }, 200);
    renderList();

    await screen.findByText(EMPTY_REQUESTS);
    const url = onlyReadUrl();
    expect(url.pathname).toBe("/api/me/requests");
    expect(url.search).toBe("");
  });

  it("a bodyless 500 renders the same read sentence", async () => {
    // `readWire` reads the body of every non-ok response to look for a typed
    // code, where `fetchRequests` reads it for no failure at all (§5's second
    // risk). A response with no body at all is where that difference shows:
    // kills a migration whose refusal branch awaits an unguarded `json()` and
    // hands the page whatever `SyntaxError` says instead of its own sentence,
    // and kills one that treats an unreadable body as a refusal carrying
    // `undefined` — `failureMessage(undefined)` is the default sentence, so the
    // `not.toContain` is what separates the two. No JSON is staged here on
    // purpose: with a body this case is the 403 above, and the point is the half
    // that has none.
    answer = () => new Response(null, { status: 500 });
    renderList();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(READ_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.default);
    expect(screen.queryByRole("list", { name: "Requests" })).toBeNull();
    expect(screen.queryByText(EMPTY_REQUESTS)).toBeNull();
  }, 10_000);
});
