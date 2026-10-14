import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GrantList } from "@/features/access/components/grant-list";
import { QueryProvider } from "@/components/layout/query-provider";
import { FAILURE_MESSAGES } from "@/features/chat/errors";
import { readAsk } from "@/features/chat/prefill";

/**
 * An earlier change §6, `access-page.test.tsx`: the `/access` list against a stubbed
 * `fetch` and a frozen clock. jsdom, no agent-api, no backing service.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23b's Deviations):
 * - the unit under test is `GrantList` from
 *   `web/src/features/access/components/grant-list.tsx`, no props, reading `/api/me/access`
 *   itself through TanStack Query (decision A), exactly as `RequestList` reads
 *   the other route.
 * - accessible names: region `Active access`, list `Grants`, and per row a
 *   link named `Request renewal for {role_name}` (§6 fixes the link's name for
 *   the fixture role; it is the role name, not the slug).
 * - the countdown reads `9 days left` (whole days, floored) and the absent one
 *   reads `No expiry`
 *   (§6's literals), and the granted date reads `2026-01-05` — the ISO prefix,
 *   the same unambiguous form the decision card and `/requests` print.
 * - the clock is frozen with `vi.useFakeTimers({ shouldAdvanceTime: true })`
 *   before `vi.setSystemTime`. `shouldAdvanceTime` is load-bearing rather than
 *   decorative: TanStack Query's retry delay and Testing Library's `waitFor`
 *   both need time to pass, and a fully frozen clock hangs every async case in
 *   this file.
 * - the empty state is §6's literal sentence, and a failed read is a
 *   `role="alert"` element.
 *
 * The wire shape is `list_my_access`' as BUILT (an earlier change, parsed by
 * `web/src/features/access/schema.ts`): `{ grants: [{grant_id, role_slug, role_name,
 * privilege, system_slug, system_name, granted_at, expires_at}] }`, with
 * `expires_at` the only nullable field, and a failed proxy answering
 * `502 {"code": "UPSTREAM_UNAVAILABLE"}`.
 */

const EMPTY_ACCESS = "You hold no active access.";
const LOGIN_URL = "http://localhost:3000/login";

/**
 * The instant every countdown below is measured from.
 *
 * A minute short of nine whole days before `EXPIRING_GRANT` ends, never exactly
 * nine: `shouldAdvanceTime` lets real milliseconds pass between `setSystemTime`
 * and the render, so a gap of exactly nine days floors to 8 by the time the row
 * is painted. The extra minute is the render's headroom, and the arithmetic the
 * countdown case asserts is unchanged.
 */
const NOW = new Date("2026-06-01T11:59:00Z");

/** A grant that ends nine whole days and a minute after `NOW`. */
const EXPIRING_GRANT: Record<string, unknown> = {
  grant_id: "55555555-5555-5555-8555-555555555555",
  role_slug: "payroll-read",
  role_name: "Fake Payroll Read",
  privilege: "read",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  granted_at: "2026-01-05T09:00:00Z",
  expires_at: "2026-06-10T12:00:00Z",
};

/** A standing grant: `expires_at` is null, which is not the same as expired. */
const PERMANENT_GRANT: Record<string, unknown> = {
  ...EXPIRING_GRANT,
  grant_id: "66666666-6666-5666-8666-666666666666",
  role_slug: "warehouse-write",
  role_name: "Fake Warehouse Write",
  privilege: "write",
  system_slug: "warehouse",
  system_name: "Fake Warehouse",
  granted_at: "2026-02-09T08:00:00Z",
  expires_at: null,
};

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

let answer: () => Response = () => jsonResponse({ grants: [] }, 200);

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The 200 a browser holds after FOLLOWING the proxy's redirect to `/login`. */
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
      <GrantList />
    </QueryProvider>,
  );
}

/** The list's own rows: its DIRECT listitem children, never a nested list's. */
function rowsOf(list: HTMLElement): HTMLElement[] {
  return within(list)
    .getAllByRole("listitem")
    .filter((item) => item.parentElement === list);
}

async function grantRows(): Promise<HTMLElement[]> {
  const region = await screen.findByRole("region", { name: "Active access" });
  return rowsOf(await within(region).findByRole("list", { name: "Grants" }));
}

/** One element of a query's result, as a value the type checker can use. */
function at(elements: HTMLElement[], index: number): HTMLElement {
  const element = elements[index];
  if (element === undefined) {
    throw new Error(`expected an element at index ${index}, found ${elements.length}`);
  }
  return element;
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(NOW);
  answer = () => jsonResponse({ grants: [] }, 200);
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : "";
    if (url.includes("/api/me/access")) {
      return answer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", { ...window.location, href: "http://localhost:3000/access", assign });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("GrantList — the rows", () => {
  it("renders one row per grant, with its role, system, privilege and granted date", async () => {
    // Catches a list rendering only the first grant, and a row that identifies
    // the grant by its SLUG — `payroll-read` on `payroll` is the wire's
    // vocabulary, and the row exists so a person can recognise what they hold.
    // The privilege assertion is the field a row is most likely to drop, and it
    // is the difference between reading payroll and writing it. The granted
    // date is asserted ISO-prefixed, which catches `toLocaleDateString`: under
    // jsdom it answers "1/5/2026", the fifth of January or the first of May
    // depending on the reader.
    answer = () => jsonResponse({ grants: [EXPIRING_GRANT, PERMANENT_GRANT] }, 200);
    renderList();

    const rows = await grantRows();
    expect(rows).toHaveLength(2);

    const row = at(rows, 0);
    expect(row).toHaveTextContent("Fake Payroll Read");
    expect(row).toHaveTextContent("Fake Payroll");
    expect(row).toHaveTextContent("read");
    expect(row).toHaveTextContent("2026-01-05");
    expect(at(rows, 1)).toHaveTextContent("write");
    expect(screen.queryByText(EMPTY_ACCESS)).toBeNull();
  });

  it("counts down in whole days, and says No expiry when there is none", async () => {
    // The clock is frozen nine days before the first grant ends, so "9 days
    // left" is arithmetic and not a coincidence. Catches a row printing the raw
    // `expires_at` where §1 asks for a countdown, and — the load-bearing half —
    // a row that treats a NULL `expires_at` as an expiry: `daysUntil(null)`
    // paths render "NaN days left" or, worse, "0 days left" against a standing
    // grant, telling the user their access ends today when it never ends. The
    // "days left" assertion on the second row is what fails for an
    // implementation that formats both the same way.
    answer = () => jsonResponse({ grants: [EXPIRING_GRANT, PERMANENT_GRANT] }, 200);
    renderList();

    const rows = await grantRows();
    expect(at(rows, 0)).toHaveTextContent("9 days left");
    expect(at(rows, 1)).toHaveTextContent("No expiry");
    expect(at(rows, 1)).not.toHaveTextContent(/days left/);
    expect(at(rows, 1).textContent).not.toContain("NaN");
    expect(at(rows, 0).textContent).not.toContain("2026-06-10");
  });

  it("gives every row a renewal link carrying the ask the composer will accept", async () => {
    // §1's renewal criterion, asserted end to end on the LINK the user clicks.
    // Catches a link named by the slug or named "Renew" on every row — with one
    // row per role, an accessible name that does not say WHICH role is a list of
    // identical links to a screen reader — and catches an href pointing at bare
    // `/chat` (decision C's rejected alternative), which drops the sentence.
    // Reading the href back through `readAsk` catches an unencoded query, which
    // would hand `/chat` a truncated ask or none at all; a link is worth nothing
    // if the composer refuses what it carries.
    answer = () => jsonResponse({ grants: [EXPIRING_GRANT, PERMANENT_GRANT] }, 200);
    renderList();

    const rows = await grantRows();
    const link = within(at(rows, 0)).getByRole("link", {
      name: "Request renewal for Fake Payroll Read",
    });
    const href = link.getAttribute("href") ?? "";

    expect(href.startsWith("/chat?ask=")).toBe(true);
    expect(readAsk(new URL(href, "http://localhost:3000").search)).toContain("Fake Payroll Read");

    expect(
      within(at(rows, 1)).getByRole("link", { name: "Request renewal for Fake Warehouse Write" }),
    ).toBeInTheDocument();
  });
});

describe("GrantList — nothing to show", () => {
  it("renders the named empty state and no list", async () => {
    // Catches a page that renders an empty list under its heading: "you hold
    // nothing" then looks exactly like a read that returned nothing because it
    // broke. The absent list is the half a sentence beside an empty `ul` would
    // not satisfy.
    answer = () => jsonResponse({ grants: [] }, 200);
    renderList();

    expect(await screen.findByText(EMPTY_ACCESS)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Grants" })).toBeNull();
  });
});

describe("GrantList — a read that fails", () => {
  const FAILURES = [
    {
      name: "a payload the parser refuses",
      answer: () => jsonResponse({ grants: [{ grant_id: "55555555-5555-5555-8555-555555" }] }, 200),
    },
    {
      name: "the proxy's own 502",
      answer: () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ] as const;

  it.each(FAILURES)(
    "$name renders an alert and no list",
    async (row) => {
      // Both rows catch a page that answers an empty array when the read fails:
      // it would tell a person they hold NO access at all — the one sentence on
      // this page a reader might act on by asking for access they already have
      // — because a proxy was down. The first row additionally catches a page
      // casting the body instead of running `safeParse` (blank rows, no error),
      // the second one that reads the body without checking the status.
      answer = row.answer;
      renderList();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toBeInTheDocument();
      expect(screen.queryByRole("list", { name: "Grants" })).toBeNull();
      expect(screen.queryByText(EMPTY_ACCESS)).toBeNull();
    },
    10_000,
  );
});

describe("GrantList — an expired session", () => {
  it("follows the proxy's redirect to /login instead of rendering rows", async () => {
    // §1's unhappy path, and the reason it is `response.redirected` and never a
    // status (22b decision E): the browser has ALREADY followed the proxy's
    // 307, so this response is a 200 carrying the login page. Catches a fetcher
    // checking `response.ok` or `response.status === 401`, both of which see a
    // successful response and leave the user on `/access` reading a parse error
    // instead of being asked to sign in again.
    answer = redirectedResponse;
    renderList();

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(screen.queryByRole("list", { name: "Grants" })).toBeNull();
  });
});

/**
 * An earlier change §6, added to 23b's file and rewriting none of it (§8): the three
 * cases that hold `fetchGrants` still, after its four private lines become
 * `readWire("/api/me/access", myAccessSchema)`.
 *
 * `READ_ALERT` is the sentence copied byte for byte out of `grant-list.tsx`,
 * which holds it as JSX text and not as a constant — reported to 24ia rather
 * than answered by exporting one this plan's §3 does not authorise.
 */
describe("GrantList — through the shared wire reader", () => {
  const READ_ALERT = "Your access could not be read. Try again in a moment.";
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
    // Decision B on the one page whose empty state a person acts on by asking
    // again for access they already hold. Through `readWire` a typed code
    // arrives as a `WireRefusedError`, which this file has never seen: kills a
    // migration that dresses it per page ("You are not allowed to do that." over
    // a list of grants the caller demonstrably holds is worse than the generic
    // sentence, and it is the alternative decision B refused), and kills one
    // that lets the class escape the error branch, leaving the page on its
    // loading state with no alert at all. The 502 rows above cannot decide this
    // — `UPSTREAM_UNAVAILABLE` is a plain `Error` through the migrated reader
    // too — so only the typed body reaches the branch under test.
    answer = () => jsonResponse({ code: "FORBIDDEN" }, 403);
    renderList();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(READ_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.FORBIDDEN);
    expect(screen.queryByRole("list", { name: "Grants" })).toBeNull();
    expect(screen.queryByText(EMPTY_ACCESS)).toBeNull();
  }, 10_000);

  it("reads /api/me/access with an empty query string", async () => {
    // The path and the query byte for byte, as an exact `pathname` and an exact
    // `search`. Kills a migration that carries the call to `/api/access` or
    // `/api/me/grants` while moving it into `readWire`, and kills one that adds
    // a parameter nobody asked for — an `?active=true` or a `?user=` would pass
    // any `toContain` check and is a project rule written into the browser.
    // Arithmetic: the staged answer is the default empty 200,
    // so reads = 1 on mount, and the empty state is what says the read settled
    // before the count is taken.
    answer = () => jsonResponse({ grants: [] }, 200);
    renderList();

    await screen.findByText(EMPTY_ACCESS);
    const url = onlyReadUrl();
    expect(url.pathname).toBe("/api/me/access");
    expect(url.search).toBe("");
  });

  it("a bodyless 500 renders the same read sentence", async () => {
    // The half `readWire`'s guarded `json()` carries and `fetchGrants` never
    // had: the migrated reader reads the body of EVERY non-ok response, so a
    // response carrying none is new ground for this page (§5's second risk).
    // Kills a refusal branch that awaits an unguarded `json()` — the page would
    // show whatever `SyntaxError` says, or nothing, for an outage — and kills
    // one that treats an unreadable body as a refusal with `undefined` as its
    // code, which `failureMessage` answers with the default sentence rather than
    // this page's own.
    answer = () => new Response(null, { status: 500 });
    renderList();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(READ_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.default);
    expect(screen.queryByRole("list", { name: "Grants" })).toBeNull();
    expect(screen.queryByText(EMPTY_ACCESS)).toBeNull();
  }, 10_000);
});
