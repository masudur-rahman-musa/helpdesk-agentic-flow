import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { REVIEW_VALIDATION_MESSAGE, ReviewQueue } from "@/features/reviews/components/review-queue";
import { FAILURE_MESSAGES } from "@/lib/failure-messages";
import {
  reviewFlagLabel,
  reviewItemDecisionLabel,
  reviewStatusLabel,
} from "@/features/reviews/labels";

/**
 * An earlier change §6, `reviews-page.test.tsx`: the `/reviews` queue, the items of the
 * selected review and the keep/revoke decision, against a stubbed `fetch`.
 * jsdom, no agent-api, no backing service. (No basename collision in the tree,
 * a project rule.)
 *
 * The unit under test is `ReviewQueue` from
 * `web/src/features/reviews/components/review-queue.tsx`, taking NO props and doing its own
 * reads and its own mutations through TanStack Query (§2, 23d decision A), so
 * `app/reviews/page.tsx` stays a thin wrapper no case here renders.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23f's Deviations):
 * - the `Status` control is a NATIVE select — located as
 *   `role="combobox"` named `Status`, its three choices as `role="option"`
 *   named `All`, `Open` and `Completed`. §1 fixes the names and the parameter
 *   each sends; it does not fix the widget, and a listbox-based one would not
 *   be findable by the same query.
 * - the option VALUES are never asserted. What `All` means is "no `status` in
 *   the url" (§1), which is asserted on the request, so the page is free to
 *   carry any value it likes behind the label.
 * - a refused DECISION's sentence is a `role="alert"` inside that ITEM's
 *   listitem; a failed item READ's sentence is an alert inside the region but
 *   outside the items list, and the failed queue read's is the region's own.
 *   The three are located differently on purpose.
 * - the review rows and the item rows are the DIRECT listitem children of
 *   their list (`rowsOf`), because `getAllByRole("listitem")` inside a list
 *   also returns every nested list's items.
 * - dates are the ISO prefix (`2026-07-01`), never `toLocaleDateString`, which
 *   under jsdom answers `7/1/2026` — a different day in half the world.
 *
 * The wire shape is 23h's as BUILT and every fixture here parses under
 * `web/src/features/reviews/schema.ts`: thirteen fields per queue row inside
 * `{ reviews: [...] }`, `{ review_id, items: [...] }` with eleven fields per
 * item, the decision's six-field answer, and a failed proxy answering
 * `502 {"code": "UPSTREAM_UNAVAILABLE"}` (`proxyAgentApi`). Exactly one of the
 * two scope pairs is non-null on any row (`access_reviews` allows one scope
 * id), which is why one fixture row is a department and the other a system.
 *
 * The labels are read through `lib/review-labels.ts` rather than retyped, the
 * way `approvals-page.test.tsx` reads `reasonSentence`: §6 fixes no label
 * text, and `review-labels.test.ts` is what pins each one as a label rather
 * than as the raw wire code.
 */

const EMPTY_QUEUE = "No reviews are waiting for you.";
const QUEUE_ALERT = "The review queue could not be read. Try again in a moment.";
const ITEMS_ALERT = "That review's items could not be read. Try again in a moment.";
const EMPTY_ITEMS = "This review has no items.";
const LOGIN_URL = "http://localhost:3000/login";

const REVIEW_ONE = "11111111-1111-5111-8111-111111111111";
const REVIEW_TWO = "22222222-2222-5222-8222-222222222222";
const ITEM_ONE = "33333333-3333-5333-8333-333333333333";
const ITEM_TWO = "44444444-4444-5444-8444-444444444444";
const ITEM_THREE = "55555555-5555-5555-8555-555555555555";
const ITEM_FOUR = "66666666-6666-5666-8666-666666666666";
const REVIEWER_ID = "77777777-7777-5777-8777-777777777777";

/** An OPEN review over a DEPARTMENT scope: the system pair is null. */
const ROW_ONE: Record<string, unknown> = {
  review_id: REVIEW_ONE,
  period: "2026-Q2",
  status: "open",
  due_at: "2026-07-01T00:00:00Z",
  completed_at: null,
  department_id: "88888888-8888-5888-8888-888888888888",
  department_name: "Fake Finance",
  system_id: null,
  system_name: null,
  reviewer_user_id: REVIEWER_ID,
  reviewer_display_name: "Fake Person One",
  item_count: 4,
  decided_count: 1,
};

/**
 * A COMPLETED review over a SYSTEM scope, with no due date: the department
 * pair is null and `due_at` is null, so the two halves §5 warns about (a blank
 * cell for the null scope pair, a blank cell for a null due date) are both
 * visible on one row.
 */
const ROW_TWO: Record<string, unknown> = {
  review_id: REVIEW_TWO,
  period: "2026-Q1",
  status: "completed",
  due_at: null,
  completed_at: "2026-04-02T00:00:00Z",
  department_id: null,
  department_name: null,
  system_id: "99999999-9999-5999-8999-999999999999",
  system_name: "Fake Warehouse",
  reviewer_user_id: REVIEWER_ID,
  reviewer_display_name: "Fake Person Two",
  item_count: 2,
  decided_count: 2,
};

/** An UNDECIDED item with an open-ended grant. */
const ITEM_A: Record<string, unknown> = {
  item_id: ITEM_ONE,
  grant_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
  flag: "sod_conflict",
  decision: null,
  decided_at: null,
  target_display_name: "Fake Person Three",
  role_name: "Fake Payroll Read",
  system_name: "Fake Payroll",
  privilege: "read",
  granted_at: "2025-11-03T09:00:00Z",
  expires_at: null,
};

/** A second UNDECIDED item, so "this item's controls and no other's" is
 * assertable: its role and target differ, so its two control names do too. */
const ITEM_B: Record<string, unknown> = {
  ...ITEM_A,
  item_id: ITEM_TWO,
  grant_id: "bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb",
  flag: "non_standard",
  target_display_name: "Fake Person Four",
  role_name: "Fake Warehouse Write",
  system_name: "Fake Warehouse",
  privilege: "write",
  expires_at: "2026-12-31T00:00:00Z",
};

/** An item already decided `keep`: §1 says it carries NEITHER control. */
const ITEM_C: Record<string, unknown> = {
  ...ITEM_A,
  item_id: ITEM_THREE,
  grant_id: "cccccccc-cccc-5ccc-8ccc-cccccccccccc",
  flag: "ok",
  decision: "keep",
  decided_at: "2026-06-10T08:00:00Z",
  target_display_name: "Fake Person Five",
  role_name: "Fake Ledger Read",
};

/** The second review's only item, whose names appear on no other item. */
const ITEM_D: Record<string, unknown> = {
  ...ITEM_A,
  item_id: ITEM_FOUR,
  grant_id: "dddddddd-dddd-5ddd-8ddd-dddddddddddd",
  flag: "stale",
  target_display_name: "Fake Person Six",
  role_name: "Fake Archive Read",
  system_name: "Fake Archive",
};

const DECISION_ANSWER: Record<string, unknown> = {
  item_id: ITEM_ONE,
  review_id: REVIEW_ONE,
  decision: "keep",
  decided_at: "2026-06-12T09:30:00Z",
  revoked_grant_count: 0,
  review_complete: false,
};

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

type Answer = () => Response | Promise<Response>;
type ItemsAnswer = (reviewId: string) => Response | Promise<Response>;

let queueAnswer: Answer = () => jsonResponse({ reviews: [] }, 200);
let itemsAnswer: ItemsAnswer = (reviewId) => jsonResponse({ review_id: reviewId, items: [] }, 200);
let decisionAnswer: Answer = () => jsonResponse(DECISION_ANSWER, 200);

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
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

function reviewIdOf(url: string): string {
  const matched = /\/api\/reviews\/([^/?]+)\/items/.exec(url);
  if (matched === null || matched[1] === undefined) {
    throw new Error(`expected an item read url, found ${url}`);
  }
  return matched[1];
}

function renderQueue(): void {
  render(
    <QueryProvider>
      <ReviewQueue />
    </QueryProvider>,
  );
}

/** The list's own rows: its DIRECT listitem children, never a nested list's. */
function rowsOf(list: HTMLElement): HTMLElement[] {
  return within(list)
    .getAllByRole("listitem")
    .filter((item) => item.parentElement === list);
}

async function region(): Promise<HTMLElement> {
  return await screen.findByRole("region", { name: "Reviews" });
}

async function reviewRows(): Promise<HTMLElement[]> {
  return rowsOf(await within(await region()).findByRole("list", { name: "Review queue" }));
}

async function itemRows(): Promise<HTMLElement[]> {
  const list = await screen.findByRole("list", { name: "Items in review" });
  return rowsOf(list);
}

/** One element of a query's result, as a value the type checker can use. */
function at(elements: HTMLElement[], index: number): HTMLElement {
  const element = elements[index];
  if (element === undefined) {
    throw new Error(`expected an element at index ${index}, found ${elements.length}`);
  }
  return element;
}

interface Call {
  url: string;
  init: RequestInit;
}

function callsMatching(predicate: (url: string) => boolean): Call[] {
  return fetchStub.mock.calls
    .map(([input, init]) => ({ url: urlOf(input), init: init ?? {} }))
    .filter((call) => predicate(call.url));
}

function queueCalls(): Call[] {
  return callsMatching((url) => url.includes("/api/me/reviews"));
}

function itemCalls(): Call[] {
  return callsMatching((url) => /\/api\/reviews\/[^/?]+\/items/.test(url));
}

function decisionCalls(): Call[] {
  return callsMatching((url) => url.includes("/api/review-items/"));
}

function callAt(calls: Call[], index: number): Call {
  const call = calls[index];
  if (call === undefined) {
    throw new Error(`expected a call at index ${index}, found ${calls.length}`);
  }
  return call;
}

function lastOf(calls: Call[]): Call {
  return callAt(calls, calls.length - 1);
}

function only(calls: Call[], what: string): Call {
  if (calls.length !== 1) {
    throw new Error(`expected exactly one ${what} call, found ${calls.length}`);
  }
  return callAt(calls, 0);
}

function headerOf(init: RequestInit, name: string): string | null {
  const headers = init.headers;
  if (headers === undefined) {
    return null;
  }
  if (headers instanceof Headers) {
    return headers.get(name);
  }
  const entries = Array.isArray(headers) ? headers : Object.entries(headers);
  const found = entries.find(([key]) => key.toLowerCase() === name);
  return found === undefined ? null : (found[1] ?? null);
}

function bodyOf(init: RequestInit): Record<string, unknown> {
  const body = init.body;
  if (typeof body !== "string") {
    throw new Error(`expected a serialised JSON body, found ${typeof body}`);
  }
  return JSON.parse(body) as Record<string, unknown>;
}

/** The queue answering both fixture rows, and the items of each review. */
function stageTwoReviews(): void {
  queueAnswer = () => jsonResponse({ reviews: [ROW_ONE, ROW_TWO] }, 200);
  itemsAnswer = (reviewId) =>
    jsonResponse(
      {
        review_id: reviewId,
        items: reviewId === REVIEW_ONE ? [ITEM_A, ITEM_B, ITEM_C] : [ITEM_D],
      },
      200,
    );
}

async function selectReview(
  user: ReturnType<typeof userEvent.setup>,
  period: string,
): Promise<void> {
  const rows = await reviewRows();
  const row = rows.find((candidate) =>
    within(candidate).queryByRole("button", { name: `Show items for ${period}` }),
  );
  if (row === undefined) {
    throw new Error(`no review row offered "Show items for ${period}"`);
  }
  await user.click(within(row).getByRole("button", { name: `Show items for ${period}` }));
}

function itemNamed(items: HTMLElement[], target: string): HTMLElement {
  const found = items.find((item) => (item.textContent ?? "").includes(target));
  if (found === undefined) {
    throw new Error(`no item row mentioned ${target}`);
  }
  return found;
}

beforeEach(() => {
  queueAnswer = () => jsonResponse({ reviews: [] }, 200);
  itemsAnswer = (reviewId) => jsonResponse({ review_id: reviewId, items: [] }, 200);
  decisionAnswer = () => jsonResponse(DECISION_ANSWER, 200);
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes("/api/me/reviews")) {
      return await queueAnswer();
    }
    if (/\/api\/reviews\/[^/?]+\/items/.test(url)) {
      return await itemsAnswer(reviewIdOf(url));
    }
    if (url.includes("/api/review-items/")) {
      return await decisionAnswer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", {
    ...window.location,
    href: "http://localhost:3000/reviews",
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ReviewQueue — the queue and its status filter", () => {
  it("reads /api/me/reviews?status=open on first paint", async () => {
    // Decision D as a request, not as a rendering. Kills a page that paints the
    // WHOLE table — after two quarters a completed review is most of it, and
    // the work waiting on the reviewer is buried — and kills one that sends
    // `status=` empty or `status=Open`, either of which reaches the tool's enum
    // as an unknown word and refuses the read the reviewer opened the page for.
    stageTwoReviews();
    renderQueue();

    await reviewRows();
    expect(only(queueCalls(), "queue").url).toContain("/api/me/reviews?status=open");
  });

  it("offers exactly All, Open and Completed under the name Status", async () => {
    // §1's third criterion as the control itself. Kills a filter offering the
    // tool's raw enum (`open`, `completed`) at the reviewer, and one with no
    // `All` at all — without it the two statuses are the only views and a
    // reviewer cannot see one screen holding both. Scoped to the combobox, so
    // any other option-shaped thing on the page cannot make it pass.
    stageTwoReviews();
    renderQueue();

    const status = await screen.findByRole("combobox", { name: "Status" });
    expect(
      within(status)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["All", "Open", "Completed"]);
  });

  const FILTERS = [
    { choice: "All", sends: null },
    { choice: "Completed", sends: "status=completed" },
  ] as const;

  it.each(FILTERS)("choosing $choice refetches the queue for it", async (filter) => {
    // Two things at once. That a SECOND read happens at all is what pins the
    // query key carrying the status (§6: `["me", "reviews", status]`) — a key
    // of `["me", "reviews"]` alone answers the cached first page and the
    // reviewer's click does nothing for thirty seconds, with no request to
    // see. And the url is what pins WHICH read: the `All` row kills a page
    // sending `status=all` or `status=` (the tool has no such member and
    // refuses both), and the `Completed` row kills a control wired to the
    // wrong value. Arithmetic: queue reads = 2, the first
    // `status=open`, the second the row's.
    stageTwoReviews();
    const user = userEvent.setup();
    renderQueue();

    await reviewRows();
    const status = await screen.findByRole("combobox", { name: "Status" });
    await user.selectOptions(status, within(status).getByRole("option", { name: filter.choice }));

    await waitFor(() => {
      expect(queueCalls()).toHaveLength(2);
    });
    const url = lastOf(queueCalls()).url;
    if (filter.sends === null) {
      expect(url).not.toContain("status");
    } else {
      expect(url).toContain(filter.sends);
    }
  });

  it("renders one row per review, with the non-null scope name, the period, the status, the due date and the progress", async () => {
    // Kills a queue rendering only `reviews[0]`, and kills four row-level
    // wrongs §5 names. A row reading `department_name` unconditionally leaves a
    // blank cell on every SYSTEM review — the fixture's second row is exactly
    // that shape, and `Fake Warehouse` is what only a page picking the non-null
    // pair renders. A row printing `review.status` raw shows the reviewer the
    // tool's vocabulary. A row computing progress as a percentage, or reading
    // only `item_count`, loses "how much is left", which is the one number this
    // screen exists to move. And the ISO assertion kills `toLocaleDateString`,
    // which under jsdom answers `7/1/2026`.
    stageTwoReviews();
    renderQueue();

    const rows = await reviewRows();
    expect(rows).toHaveLength(2);

    const first = at(rows, 0);
    expect(first).toHaveTextContent("Fake Finance");
    expect(first).toHaveTextContent("2026-Q2");
    expect(first).toHaveTextContent(reviewStatusLabel("open"));
    expect(first).toHaveTextContent("2026-07-01");
    expect(first).toHaveTextContent("Fake Person One");
    expect(first).toHaveTextContent("1 of 4 decided");
    expect(first.textContent ?? "").not.toContain("7/1/2026");

    const second = at(rows, 1);
    expect(second).toHaveTextContent("Fake Warehouse");
    expect(second).toHaveTextContent("2026-Q1");
    expect(second).toHaveTextContent(reviewStatusLabel("completed"));
    expect(second).toHaveTextContent("Fake Person Two");
    expect(second).toHaveTextContent("2 of 2 decided");
    expect(screen.queryByText(EMPTY_QUEUE)).toBeNull();
  });

  it("reads no due date for a review whose due_at is null", async () => {
    // The null half of §1's second criterion, on the row that carries it.
    // Kills a page interpolating the null — `null` or `Invalid Date` in the
    // due column — and one leaving the cell empty, where a review with no
    // deadline looks exactly like a rendering bug and a reviewer cannot tell
    // whether they are late. Scoped to the second row, because the first row
    // HAS a due date and would hide the fault.
    stageTwoReviews();
    renderQueue();

    const second = at(await reviewRows(), 1);
    expect(second).toHaveTextContent("no due date");
    expect(second.textContent ?? "").not.toContain("null");
    expect(second.textContent ?? "").not.toContain("Invalid Date");
  });
});

describe("ReviewQueue — reading one review's items", () => {
  it("reads no items at all until a review is selected", async () => {
    // §5's first risk, from the side that is invisible once it ships: a child
    // mounting its query for EVERY row turns a queue of fifty into fifty calls
    // on first paint, each one an MCP round trip and a rate-limit slot, and the
    // page still looks right. Only the absence of a call catches it, and it has
    // to be asserted after the rows have rendered or an unmounted list would
    // pass it trivially.
    stageTwoReviews();
    renderQueue();

    expect(await reviewRows()).toHaveLength(2);
    expect(itemCalls()).toHaveLength(0);
    expect(screen.queryByRole("list", { name: "Items in review" })).toBeNull();
  });

  it("selecting a review reads that review's items exactly once", async () => {
    // The other side of §5's first risk, and the kill for a child keyed on
    // nothing: the url must carry the SELECTED review's id, so a page closing
    // over the first row (or over a loop variable) reads the wrong review's
    // items and shows the reviewer four grants that belong to someone else's
    // scope. The "exactly once" count kills a query mounted twice or refetched
    // on every render.
    stageTwoReviews();
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");

    await waitFor(() => {
      expect(itemCalls()).toHaveLength(1);
    });
    expect(only(itemCalls(), "item").url).toContain(`/api/reviews/${REVIEW_ONE}/items`);
    expect(only(itemCalls(), "item").url).not.toContain(REVIEW_TWO);
  });

  it("selecting a second review replaces the first review's items", async () => {
    // §1's "exactly ONE review's items are read at a time". Kills a page that
    // accumulates expanded lists: two `Items in review` lists on screen, or one
    // list holding both reviews' items, puts a grant from another scope beside
    // the Keep and Revoke controls of this one — the reviewer decides on the
    // wrong row and the tool happily files it. The absence of `Fake Person
    // Three` is the load-bearing half; a page appending rather than replacing
    // passes every other assertion here. Arithmetic: item reads = 2, one per
    // selection, the query key being `["reviews", reviewId, "items"]`.
    stageTwoReviews();
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");
    await waitFor(async () => {
      expect(await itemRows()).toHaveLength(3);
    });

    await selectReview(user, "2026-Q1");
    await waitFor(async () => {
      expect(await itemRows()).toHaveLength(1);
    });
    expect(screen.getAllByRole("list", { name: "Items in review" })).toHaveLength(1);
    expect(at(await itemRows(), 0)).toHaveTextContent("Fake Person Six");
    expect(screen.queryByText("Fake Person Three")).toBeNull();
    expect(itemCalls()).toHaveLength(2);
    expect(lastOf(itemCalls()).url).toContain(`/api/reviews/${REVIEW_TWO}/items`);
  });

  it("renders each item with its flag label, its names, its privilege and its dates", async () => {
    // §1's fourth criterion, one assertion per field, and the kill for a list
    // that renders the wire: `sod_conflict` in front of a reviewer explains
    // nothing and is the exact drift `lib/review-labels.ts` exists to stop, so
    // the raw code's ABSENCE is asserted beside the label's presence. The
    // privilege and the system are what separate two grants of the same role
    // name; the granted prefix kills `toLocaleDateString` again (jsdom:
    // `11/3/2025`); and `no expiry` kills a page interpolating a null, where an
    // open-ended grant — the one most worth revoking — reads as a blank cell.
    stageTwoReviews();
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");
    const items = await itemRows();
    expect(items).toHaveLength(3);

    const first = itemNamed(items, "Fake Person Three");
    expect(first).toHaveTextContent(reviewFlagLabel("sod_conflict"));
    expect(first).toHaveTextContent("Fake Payroll Read");
    expect(first).toHaveTextContent("Fake Payroll");
    expect(first).toHaveTextContent("read");
    expect(first).toHaveTextContent("2025-11-03");
    expect(first).toHaveTextContent("no expiry");
    expect(first.textContent ?? "").not.toContain("sod_conflict");
    expect(first.textContent ?? "").not.toContain("11/3/2025");

    const second = itemNamed(items, "Fake Person Four");
    expect(second).toHaveTextContent(reviewFlagLabel("non_standard"));
    expect(second).toHaveTextContent("2026-12-31");
    expect(second.textContent ?? "").not.toContain("non_standard");
  });

  it("shows a decided item's decision label and gives it neither control", async () => {
    // §1's seventh criterion, both halves on the one item that has them. Kills
    // a list offering Keep and Revoke on an item the tool has already decided:
    // the second decision is refused upstream, so what the reviewer gets for
    // clicking is an error sentence on a row that was never theirs to act on,
    // and the progress count never moves. The decision label's presence is the
    // other half — an item with no controls and no label is
    // indistinguishable from one the page simply failed to render controls
    // for.
    stageTwoReviews();
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");
    const decided = itemNamed(await itemRows(), "Fake Person Five");

    expect(decided).toHaveTextContent(reviewItemDecisionLabel("keep"));
    expect(
      within(decided).queryByRole("button", { name: "Keep Fake Ledger Read for Fake Person Five" }),
    ).toBeNull();
    expect(
      within(decided).queryByRole("button", {
        name: "Revoke Fake Ledger Read for Fake Person Five",
      }),
    ).toBeNull();
  });
});

describe("ReviewQueue — filing a decision", () => {
  const DECISIONS = [
    { verb: "Keep", decision: "keep" },
    { verb: "Revoke", decision: "revoke" },
  ] as const;

  it.each(DECISIONS)(
    '$verb posts {"decision":"$decision"} to that item\'s decide proxy',
    async (row) => {
      // Kills a control wired to the other word — a Revoke posting `keep`
      // leaves live access in place on a grant a human just said to remove, and
      // nothing on this page would look wrong — and kills one posting the
      // other ITEM's id, the shape a handler closing over a loop variable
      // produces. The exact key set kills a page inventing an idempotency key
      // in the body: agent-api derives it from the verified reviewer, the item
      // and the word (23h), so a client-chosen one is both ignored and a
      // client-supplied string the page has no business minting. Method and
      // content type are asserted because the proxy forwards the body verbatim
      // and a form-encoded post is refused upstream.
      stageTwoReviews();
      const user = userEvent.setup();
      renderQueue();

      await selectReview(user, "2026-Q2");
      const item = itemNamed(await itemRows(), "Fake Person Three");
      await user.click(
        within(item).getByRole("button", {
          name: `${row.verb} Fake Payroll Read for Fake Person Three`,
        }),
      );

      await waitFor(() => {
        expect(decisionCalls()).toHaveLength(1);
      });
      const call = only(decisionCalls(), "decision");
      expect(call.url).toContain(`/api/review-items/${ITEM_ONE}/decide`);
      expect(call.url).not.toContain(ITEM_TWO);
      expect(call.init.method).toBe("POST");
      expect(headerOf(call.init, "content-type")).toBe("application/json");
      expect(bodyOf(call.init)).toEqual({ decision: row.decision });
    },
  );

  it("disables both controls of the deciding item while its decision is in flight, and no other item's", async () => {
    // §1's seventh criterion. Kills a page holding ONE in-flight flag for the
    // whole list — every control greys out while one item is being decided, so
    // a reviewer with forty grants waits for each — and a page with no flag at
    // all, where the Revoke beside an in-flight Keep is still live and files
    // the opposite decision on the same item. The second item's two ENABLED
    // controls are the half a global flag fails; the deciding item's disabled
    // Revoke is the half no flag fails.
    stageTwoReviews();
    let release: (value: Response) => void = () => {};
    decisionAnswer = () =>
      new Promise<Response>((resolve) => {
        release = resolve;
      });
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");
    const items = await itemRows();
    const deciding = itemNamed(items, "Fake Person Three");
    const other = itemNamed(items, "Fake Person Four");
    await user.click(
      within(deciding).getByRole("button", {
        name: "Keep Fake Payroll Read for Fake Person Three",
      }),
    );

    await waitFor(() => {
      expect(
        within(deciding).getByRole("button", {
          name: "Keep Fake Payroll Read for Fake Person Three",
        }),
      ).toBeDisabled();
    });
    expect(
      within(deciding).getByRole("button", {
        name: "Revoke Fake Payroll Read for Fake Person Three",
      }),
    ).toBeDisabled();
    expect(
      within(other).getByRole("button", {
        name: "Keep Fake Warehouse Write for Fake Person Four",
      }),
    ).toBeEnabled();
    expect(
      within(other).getByRole("button", {
        name: "Revoke Fake Warehouse Write for Fake Person Four",
      }),
    ).toBeEnabled();

    release(jsonResponse(DECISION_ANSWER, 200));
    await waitFor(() => {
      expect(
        within(deciding).getByRole("button", {
          name: "Keep Fake Payroll Read for Fake Person Three",
        }),
      ).toBeEnabled();
    });
  });

  it("refetches both the items and the queue after a decision, so the progress moves", async () => {
    // §5's third risk, which is the one an item-only invalidation ships
    // silently: the counts live on the QUEUE payload, so a page invalidating
    // `["reviews", id, "items"]` alone leaves `1 of 4 decided` sitting under an
    // item the reviewer just decided, and the only way to see the truth is a
    // manual reload. Kills that, and kills a mutation with no invalidation at
    // all (the item would keep its two controls). The progress assertion reads
    // the refetch's OWN answer, never an optimistic guess — §1 says
    // `review_complete` and the new decision are only ever what the refetch
    // says. Arithmetic: queue reads 1 -> 2, item reads
    // 1 -> 2, decision calls = 1; the second queue read answers `2 of 4`.
    let queueReads = 0;
    queueAnswer = () => {
      queueReads += 1;
      const row = queueReads === 1 ? ROW_ONE : { ...ROW_ONE, decided_count: 2 };
      return jsonResponse({ reviews: [row, ROW_TWO] }, 200);
    };
    itemsAnswer = (reviewId) =>
      jsonResponse({ review_id: reviewId, items: [ITEM_A, ITEM_B, ITEM_C] }, 200);
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");
    const item = itemNamed(await itemRows(), "Fake Person Three");
    expect(at(await reviewRows(), 0)).toHaveTextContent("1 of 4 decided");

    await user.click(
      within(item).getByRole("button", { name: "Keep Fake Payroll Read for Fake Person Three" }),
    );

    await waitFor(() => {
      expect(queueCalls()).toHaveLength(2);
    });
    await waitFor(() => {
      expect(itemCalls()).toHaveLength(2);
    });
    await waitFor(async () => {
      expect(at(await reviewRows(), 0)).toHaveTextContent("2 of 4 decided");
    });
    expect(decisionCalls()).toHaveLength(1);
  });
});

describe("ReviewQueue — a refused decision", () => {
  const REFUSALS = [
    { name: "FORBIDDEN", status: 403, code: "FORBIDDEN", sentence: FAILURE_MESSAGES.FORBIDDEN },
    {
      name: "TOOL_DISABLED",
      status: 503,
      code: "TOOL_DISABLED",
      sentence: FAILURE_MESSAGES.TOOL_DISABLED,
    },
    { name: "VALIDATION", status: 400, code: "VALIDATION", sentence: REVIEW_VALIDATION_MESSAGE },
  ] as const;

  it.each(REFUSALS)(
    "$name renders its sentence in that item's alert and empties neither list",
    async (refusal) => {
      // Three kills in one case. A page rendering the CODE at the reviewer
      // ("TOOL_DISABLED" means nothing to the person reading it) — the
      // sentences are READ from `FAILURE_MESSAGES` rather than retyped, so a
      // page inventing its own wording for `FORBIDDEN` is red without widening
      // 22b's pinned map. A page whose mutation error clears the rows the two
      // reads answered: the counts after the refusal are what catch a queue or
      // an item list emptied by a 403. And, on the VALIDATION row, a page that
      // used the SHARED sentence — "That message could not be sent." is about a
      // chat message and says nothing true about a refused decision, which is
      // the whole of decision E; that row asserts the override AND the absence
      // of the shared string, so reusing the map is red in both directions. The
      // alert is located INSIDE the item, so a page showing one refusal banner
      // for the whole list (the reviewer cannot tell which of forty items was
      // refused) fails here too.
      stageTwoReviews();
      decisionAnswer = () => jsonResponse({ code: refusal.code }, refusal.status);
      const user = userEvent.setup();
      renderQueue();

      await selectReview(user, "2026-Q2");
      const item = itemNamed(await itemRows(), "Fake Person Three");
      await user.click(
        within(item).getByRole("button", { name: "Keep Fake Payroll Read for Fake Person Three" }),
      );

      const alert = await within(item).findByRole("alert");
      expect(alert).toHaveTextContent(refusal.sentence);
      expect(await itemRows()).toHaveLength(3);
      expect(await reviewRows()).toHaveLength(2);
      expect(screen.queryByText(QUEUE_ALERT)).toBeNull();
      expect(screen.queryByText(ITEMS_ALERT)).toBeNull();
      if (refusal.code === "VALIDATION") {
        expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.VALIDATION);
      }
    },
  );

  it("pins the VALIDATION override sentence exactly", async () => {
    // a project rule on the one string this plan introduces. Kills an
    // implementation that exports the constant but fills it with the shared
    // map's sentence, or with a reworded variant: the parametrised row above
    // READS the constant, so it stays green for any value the module chooses,
    // and only this literal fixes it. Awaits nothing — the constant is the
    // whole subject.
    expect(REVIEW_VALIDATION_MESSAGE).toBe(
      "That decision could not be filed. Reload the page and try again.",
    );
    expect(REVIEW_VALIDATION_MESSAGE).not.toBe(FAILURE_MESSAGES.VALIDATION);
  });
});

describe("ReviewQueue — nothing to show, and reads that fail", () => {
  it("renders the named empty queue with no list and no alert", async () => {
    // Kills a page rendering an empty list under its heading: "no reviews are
    // waiting" then looks exactly like a read that returned nothing because it
    // broke. The absent alert is the other half of §1's mutual exclusion — a
    // page showing both says two contradictory things at once.
    queueAnswer = () => jsonResponse({ reviews: [] }, 200);
    renderQueue();

    expect(await screen.findByText(EMPTY_QUEUE)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Review queue" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  const QUEUE_FAILURES = [
    {
      name: "a payload the parser refuses",
      answer: (): Response => jsonResponse({ reviews: [{ review_id: REVIEW_ONE }] }, 200),
    },
    {
      name: "the proxy's own 502",
      answer: (): Response => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ] as const;

  it.each(QUEUE_FAILURES)(
    "$name renders the queue alert, no list and no empty state",
    async (row) => {
      // Both rows kill one wrong implementation from two directions: a fetcher
      // answering an empty array when the read fails. It would print "No
      // reviews are waiting for you." over a quarter's worth of access nobody
      // has looked at — the one sentence on this page a reviewer acts on by
      // closing the tab — because a proxy was down. The first row additionally
      // kills a page that CASTS the body instead of running `safeParse` (the
      // rows draw with blank cells and no error); the second kills one reading
      // the body without checking the status, since a 502 envelope is valid
      // JSON. The sentence is pinned verbatim so a generic "something went
      // wrong" cannot stand in for it.
      queueAnswer = row.answer;
      renderQueue();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(QUEUE_ALERT);
      expect(screen.queryByRole("list", { name: "Review queue" })).toBeNull();
      expect(screen.queryByText(EMPTY_QUEUE)).toBeNull();
    },
    10_000,
  );

  it("renders the named empty item state for a selected review with no items", async () => {
    // A review whose scope had no live grants is an ordinary outcome, not a
    // fault. Kills a page rendering an empty `Items in review` list (the
    // reviewer sees a heading and white space and reloads), and kills one
    // showing the item ALERT for an empty list, which would have them chasing
    // an outage that is not happening. The absent alert is asserted inside the
    // region so the queue's own state cannot satisfy it.
    stageTwoReviews();
    itemsAnswer = (reviewId) => jsonResponse({ review_id: reviewId, items: [] }, 200);
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");

    expect(await screen.findByText(EMPTY_ITEMS)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Items in review" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  const ITEM_FAILURES = [
    {
      name: "a payload the parser refuses",
      answer: (reviewId: string): Response =>
        jsonResponse({ review_id: reviewId, items: [{ item_id: ITEM_ONE }] }, 200),
    },
    {
      name: "the proxy's own 502",
      answer: (): Response => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ] as const;

  it.each(ITEM_FAILURES)(
    "$name renders the item alert while the queue stays rendered",
    async (row) => {
      // §1's second unhappy path, and the half that makes it its OWN sentence:
      // a failed item read must not read like a failed queue read, because the
      // queue on screen is fine and only the selected review is unreadable. The
      // queue rows still being there is what kills a page whose item error
      // tears down the whole region; the absence of the QUEUE sentence kills
      // one reusing the other alert's wording, which would send the reviewer
      // reloading a page that has nothing wrong with it.
      stageTwoReviews();
      itemsAnswer = row.answer;
      const user = userEvent.setup();
      renderQueue();

      await selectReview(user, "2026-Q2");

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(ITEMS_ALERT);
      expect(screen.queryByText(QUEUE_ALERT)).toBeNull();
      expect(screen.queryByText(EMPTY_ITEMS)).toBeNull();
      expect(await reviewRows()).toHaveLength(2);
    },
    10_000,
  );
});

describe("ReviewQueue — an expired session", () => {
  it("follows the proxy's redirect to /login instead of rendering the queue", async () => {
    // §1's last unhappy path at the QUEUE read, and the reason it is
    // `response.redirected` and never a status (22b decision E, standing rule
    // 59): the browser has ALREADY followed the proxy's redirect, so what
    // arrives is a 200 carrying the login page. Kills a fetcher checking
    // `response.ok` or `response.status === 401` — both see a successful
    // response, the parser then refuses the HTML, and the reviewer sits on
    // `/reviews` reading a read error instead of being asked to sign in.
    queueAnswer = redirectedResponse;
    renderQueue();

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(screen.queryByRole("list", { name: "Review queue" })).toBeNull();
  });

  it("follows the redirect at the item read too", async () => {
    // The half a fetcher fixed in one place misses, at the second of the three
    // call sites. The queue read here SUCCEEDS, so only the item path can make
    // the assignment happen and no read-side handling can stand in for it. A
    // page missing it shows the reviewer the item alert for a session that
    // merely expired, which is the one failure a reload would have fixed.
    stageTwoReviews();
    itemsAnswer = redirectedResponse;
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
  });

  it("follows the redirect at the decision too", async () => {
    // The third call site, and the likeliest one: a review queue sits open on a
    // screen for many minutes before anyone clicks Keep. A mutation ignoring
    // `redirected` parses the login HTML, throws, and shows a refusal sentence
    // for a decision that was never filed — the reviewer believes they decided
    // an item they did not. Both reads succeed here, so only the decision path
    // can satisfy this.
    stageTwoReviews();
    decisionAnswer = redirectedResponse;
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");
    const item = itemNamed(await itemRows(), "Fake Person Three");
    await user.click(
      within(item).getByRole("button", { name: "Keep Fake Payroll Read for Fake Person Three" }),
    );

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
  });
});

/**
 * An earlier change §6, added to 23f's file and rewriting none of it (§8): three cases
 * per read for the TWO reads this component owns — the queue
 * (`readWire("/api/me/reviews?status=...", reviewQueueSchema)`) and the items
 * (`readWire("/api/reviews/{id}/items", reviewItemsSchema)`). The decision POST
 * is not a read and keeps its own `DecisionRefusedError`, so no case here
 * clicks Keep or Revoke.
 */
describe("ReviewQueue — the queue read through the shared wire reader", () => {
  const ORIGIN = "http://localhost:3000";

  it("a 403 FORBIDDEN on the queue renders the queue alert, not the code's sentence", async () => {
    // Decision B on the surface that already renders `FAILURE_MESSAGES`
    // sentences for a refused DECISION, so reusing that dressing for the READ is
    // one import away. Kills exactly that migration — "You are not allowed to do
    // that." in the region alert reads as "you may not review", when what failed
    // is the listing — and kills a `WireRefusedError` escaping the error branch,
    // which leaves the page on its loading state with neither alert nor queue.
    // The two rows above cannot decide it: a refused payload and a 502 are plain
    // errors through the migrated reader, so only a typed body reaches the new
    // class.
    queueAnswer = () => jsonResponse({ code: "FORBIDDEN" }, 403);
    renderQueue();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(QUEUE_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.FORBIDDEN);
    expect(screen.queryByRole("list", { name: "Review queue" })).toBeNull();
    expect(screen.queryByText(EMPTY_QUEUE)).toBeNull();
  }, 10_000);

  it("reads /api/me/reviews with the exact query string ?status=open", async () => {
    // The one read of the eight whose query string is not empty, asserted as an
    // exact `search` rather than as `toContain` (the case above this file's
    // fixtures asserts it that way, and `?status=open&limit=50` would satisfy
    // it). Kills a migration that drops the filter while moving the call into
    // `readWire` — decision D's first paint is the OPEN reviews, and after two
    // quarters the whole table is mostly completed ones — and kills one that
    // sends `?status=` or `?status=Open`, either of which reaches the tool's
    // enum as a word it has no member for. Arithmetic: the
    // staged answer is the default empty 200, so queue reads = 1 on mount and
    // item reads = 0 with no row selected.
    queueAnswer = () => jsonResponse({ reviews: [] }, 200);
    renderQueue();

    await screen.findByText(EMPTY_QUEUE);
    const url = new URL(only(queueCalls(), "queue").url, ORIGIN);
    expect(url.pathname).toBe("/api/me/reviews");
    expect(url.search).toBe("?status=open");
    expect(itemCalls()).toHaveLength(0);
  });

  it("a bodyless 500 on the queue renders the same queue alert", async () => {
    // §5's second risk at the first of this component's two reads: `readWire`
    // reads the body of every non-ok response looking for a typed code, and
    // `fetchReviews` reads it for none. Kills a refusal branch awaiting an
    // unguarded `json()`, and kills one that treats an unreadable body as a
    // refusal carrying `undefined` — `failureMessage(undefined)` is the default
    // sentence, which says nothing about a queue.
    queueAnswer = () => new Response(null, { status: 500 });
    renderQueue();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(QUEUE_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.default);
    expect(screen.queryByRole("list", { name: "Review queue" })).toBeNull();
    expect(screen.queryByText(EMPTY_QUEUE)).toBeNull();
  }, 10_000);
});

describe("ReviewQueue — the item read through the shared wire reader", () => {
  const ORIGIN = "http://localhost:3000";

  it("a 403 FORBIDDEN on the items renders the item alert, not the code's sentence", async () => {
    // The same decision B at the SECOND read, which a migration fixed in one
    // place misses. The queue read succeeds here, so only the item path can
    // produce this alert and the queue's own error branch cannot stand in for
    // it. Kills an item reader dressing the typed code (the reviewer would be
    // told they are not allowed to do something, while the queue they are
    // looking at renders fine), kills one reusing the QUEUE sentence — which
    // sends them reloading a page that has nothing wrong with it — and kills a
    // `WireRefusedError` escaping into no alert at all, which after a click on
    // Show items leaves the row silent.
    stageTwoReviews();
    itemsAnswer = () => jsonResponse({ code: "FORBIDDEN" }, 403);
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(ITEMS_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.FORBIDDEN);
    expect(screen.queryByText(QUEUE_ALERT)).toBeNull();
    expect(screen.queryByText(EMPTY_ITEMS)).toBeNull();
    expect(await reviewRows()).toHaveLength(2);
  }, 10_000);

  it("reads /api/reviews/{review_id}/items with an empty query string", async () => {
    // The path of the one read whose url carries an id, asserted as an exact
    // `pathname` holding the SELECTED review's id and an exact empty `search`.
    // Kills a migration that moves the id into a query string
    // (`/api/reviews/items?review_id=...` hits no route this repo has), kills
    // one that sends the other row's id — the queue holds two reviews and only
    // `2026-Q2` was clicked, so a reader keyed on "the first row" is red here —
    // and kills one that adds a filter or a page size to a read the reviewer
    // decides items from. Arithmetic: queue reads = 1 on
    // mount, item reads = 1 after exactly one Show items click.
    stageTwoReviews();
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");
    await itemRows();

    const url = new URL(only(itemCalls(), "item").url, ORIGIN);
    expect(url.pathname).toBe(`/api/reviews/${REVIEW_ONE}/items`);
    expect(url.search).toBe("");
  });

  it("a bodyless 500 on the items renders the same item alert", async () => {
    // §5's second risk at the second read. `fetchItems` is the one of the eight
    // that reads no body on failure at all today, so a migration routing it
    // through `readWire` newly parses one; a response carrying none is where
    // that shows. Kills an unguarded `json()` in the refusal branch, and kills
    // an unreadable body classified as a refusal with `undefined` as its code,
    // which renders the default sentence rather than the item one. The queue
    // read succeeds, so the queue's alert cannot satisfy this case.
    stageTwoReviews();
    itemsAnswer = () => new Response(null, { status: 500 });
    const user = userEvent.setup();
    renderQueue();

    await selectReview(user, "2026-Q2");

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(ITEMS_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.default);
    expect(screen.queryByText(QUEUE_ALERT)).toBeNull();
    expect(await reviewRows()).toHaveLength(2);
  }, 10_000);
});
