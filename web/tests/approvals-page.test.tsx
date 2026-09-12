import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApprovalQueue, DECISION_VALIDATION_MESSAGE } from "@/components/approvals/approval-queue";
import { QueryProvider } from "@/components/query-provider";
import { FAILURE_MESSAGES } from "@/lib/chat-errors";
import { reasonSentence } from "@/lib/decision-labels";

/**
 * An earlier change §6, `approvals-page.test.tsx`: the `/approvals` queue and its two
 * decisions against a stubbed `fetch`. jsdom, no agent-api, no backing
 * service. (No basename collision in the tree — a project rule.)
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23d's Deviations):
 * - the unit under test is `ApprovalQueue` from
 *   `web/components/approvals/approval-queue.tsx`, taking **no props** and
 *   doing its own read of `/api/me/approvals` and its own two posts through
 *   TanStack Query (decision A) — `RequestList`'s shape, so
 *   `app/approvals/page.tsx` stays a thin wrapper a test never has to await.
 * - the `VALIDATION` override of decision B is exported from that same module
 *   as `DECISION_VALIDATION_MESSAGE`, and its value is §6's sentence,
 *   `That decision could not be filed. Check the comment and try again.`
 *   It is declared beside the component precisely so this file can import it
 *   rather than repeat it, and so no second opinion about any OTHER code can
 *   appear: every other sentence is read out of `FAILURE_MESSAGES`.
 * - accessible names: region `Approvals`, the row list `Pending approvals`,
 *   and inside a row the three nested lists `Reasons`, `Approvers` and
 *   `Sources` (§6). A row holding three lists is otherwise not countable.
 * - a row's listitems are its DIRECT children of the `Pending approvals` list
 *   (`rowsOf`), because `getAllByRole("listitem")` inside a list also returns
 *   every nested list's items.
 * - the per-row controls are named with the role name, the `grant-list.tsx`
 *   shape: textboxes `Comment for <role_name>` and `Reason for <role_name>`,
 *   buttons `Approve <role_name>` and `Reject <role_name>`.
 * - a refused decision's sentence is a `role="alert"` INSIDE that row; the
 *   failed READ's sentence is the region-level alert, and the two are located
 *   differently on purpose.
 * - dates are the ISO prefix (`2026-06-02`), never `toLocaleDateString`.
 *
 * The wire shape is 23c's as BUILT: twenty fields per row inside
 * `{ approvals: [...] }` (parsed by `web/lib/approvals-schema.ts`), a citation
 * being `{document_slug, title, version, heading_path, chunk_id}` with
 * `heading_path` a STRING, a seat being `{slot, user_id}` with `user_id` null
 * for an unresolved group seat, and a failed proxy answering
 * `502 {"code": "UPSTREAM_UNAVAILABLE"}` (`proxyAgentApi`).
 */

const EMPTY_QUEUE = "Nothing is waiting for your decision.";
const READ_ALERT = "The approval queue could not be read. Try again in a moment.";
const LOGIN_URL = "http://localhost:3000/login";

/** The tool's cap (23c decision E). The page declares no bound of its own. */
const COMMENT_MAX_LENGTH = 2000;

const REQUEST_ONE = "66666666-6666-5666-8666-666666666666";
const REQUEST_TWO = "88888888-8888-5888-8888-888888888888";
const REQUESTER_ID = "11111111-1111-5111-8111-111111111111";
const TARGET_ID = "22222222-2222-5222-8222-222222222222";
const RULE_ID = "33333333-3333-5333-8333-333333333333";
const SEAT_USER_ID = "44444444-4444-5444-8444-444444444444";

/**
 * A pending approval whose chain has two seats, the caller's being the SECOND
 * (`my_slot` is `it_admin`) and the resolved one. The first seat is the
 * unresolved group seat. That arrangement is deliberate: a page marking the
 * first seat, marking every seat, or marking the seat with no `user_id` all
 * mark the WRONG one here, and only a page reading `my_slot` marks seat two.
 */
const ROW_ONE: Record<string, unknown> = {
  request_id: REQUEST_ONE,
  requester_user_id: REQUESTER_ID,
  target_user_id: TARGET_ID,
  role_id: "77777777-7777-5777-8777-777777777777",
  justification: "Obviously fake justification.",
  requested_expiry_days: 30,
  risk_class: "elevated",
  decision: "approval",
  status: "pending",
  created_at: "2026-06-02T10:15:00Z",
  my_slot: "it_admin",
  approvals: [
    { slot: "department_manager", user_id: null },
    { slot: "it_admin", user_id: SEAT_USER_ID },
  ],
  role_slug: "payroll-read",
  role_name: "Fake Payroll Read",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  target_display_name: "Fake Person One",
  requester_display_name: "Fake Person Two",
  decision_reasons: ["NOT_IN_TEMPLATE", `SOD_CONFLICT:${RULE_ID}`],
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
};

/** A second row, so "one row per payload row" and "the OTHER row's controls
 * are still enabled" are both assertable. Its single seat is the caller's. */
const ROW_TWO: Record<string, unknown> = {
  ...ROW_ONE,
  request_id: REQUEST_TWO,
  requested_expiry_days: null,
  risk_class: "critical",
  my_slot: "system_owner",
  approvals: [{ slot: "system_owner", user_id: SEAT_USER_ID }],
  role_slug: "warehouse-write",
  role_name: "Fake Warehouse Write",
  system_slug: "warehouse",
  system_name: "Fake Warehouse",
  decision_reasons: ["PRIVILEGE_ADMIN"],
  citations: [],
};

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

type Answer = () => Response | Promise<Response>;

let answer: Answer = () => jsonResponse({ approvals: [] }, 200);
let decisionAnswer: Answer = () => jsonResponse({ status: "approved" }, 200);

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

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.toString();
  }
  return input.url;
}

function renderQueue(): void {
  render(
    <QueryProvider>
      <ApprovalQueue />
    </QueryProvider>,
  );
}

/** The list's own rows: its DIRECT listitem children, never a nested list's. */
function rowsOf(list: HTMLElement): HTMLElement[] {
  return within(list)
    .getAllByRole("listitem")
    .filter((item) => item.parentElement === list);
}

async function approvalRows(): Promise<HTMLElement[]> {
  const region = await screen.findByRole("region", { name: "Approvals" });
  return rowsOf(within(region).getByRole("list", { name: "Pending approvals" }));
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

interface Call {
  url: string;
  init: RequestInit;
}

/** Every POST the page made to a decision proxy, in order. */
function decisionCalls(): Call[] {
  return fetchStub.mock.calls
    .map(([input, init]) => ({ url: urlOf(input), init: init ?? {} }))
    .filter((call) => call.url.includes("/api/approvals/"));
}

function readCallCount(): number {
  return fetchStub.mock.calls.filter(([input]) => urlOf(input).includes("/api/me/approvals"))
    .length;
}

function only(calls: Call[]): Call {
  if (calls.length !== 1) {
    throw new Error(`expected exactly one decision call, found ${calls.length}`);
  }
  return callAt(calls, 0);
}

function callAt(calls: Call[], index: number): Call {
  const call = calls[index];
  if (call === undefined) {
    throw new Error(`expected a call at index ${index}, found ${calls.length}`);
  }
  return call;
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

beforeEach(() => {
  answer = () => jsonResponse({ approvals: [] }, 200);
  decisionAnswer = () => jsonResponse({ status: "approved" }, 200);
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes("/api/me/approvals")) {
      return await answer();
    }
    if (url.includes("/api/approvals/")) {
      return await decisionAnswer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", {
    ...window.location,
    href: "http://localhost:3000/approvals",
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ApprovalQueue — the decision trace on a row", () => {
  it("renders one row per pending approval, with the names, the labels, the reasons and the sources", async () => {
    // Kills a queue that renders only `approvals[0]` (an approver would then
    // never see the second thing waiting on them), and inside the row kills
    // four separate wrong implementations: one that prints the engine's
    // vocabulary at the user (`approval`, `elevated`, `pending` are wire
    // values, not sentences); one that identifies the requester or the target
    // by uuid, which the design asks for by display name because a uuid tells an
    // approver nothing about who they are granting access to; one that prints
    // the raw reason codes or interpolates the SoD rule id back in; and one
    // that drops a citation's VERSION, after which two versions of one policy
    // read identically and the approver cannot tell which text the engine
    // decided against. The Sources count is scoped to the row's own `Sources`
    // list, so the reasons and the seats cannot make it pass, and the ISO date
    // assertion kills `toLocaleDateString`, which under jsdom answers
    // "6/2/2026" — a different day in half the world.
    answer = () => jsonResponse({ approvals: [ROW_ONE, ROW_TWO] }, 200);
    renderQueue();

    const rows = await approvalRows();
    expect(rows).toHaveLength(2);

    const row = at(rows, 0);
    expect(row).toHaveTextContent("Fake Payroll Read");
    expect(row).toHaveTextContent("Fake Payroll");
    expect(row).toHaveTextContent("Fake Person One");
    expect(row).toHaveTextContent("Fake Person Two");
    expect(row).toHaveTextContent("Needs approval");
    expect(row).toHaveTextContent("Elevated risk");
    expect(row).toHaveTextContent("2026-06-02");
    expect(row.textContent ?? "").not.toContain(TARGET_ID);
    expect(row.textContent ?? "").not.toContain(REQUESTER_ID);
    expect(row.textContent ?? "").not.toContain("6/2/2026");

    const reasons = itemsOf(row, "Reasons");
    expect(reasons).toHaveLength(2);
    expect(reasons[0]).toHaveTextContent(reasonSentence("NOT_IN_TEMPLATE"));
    expect(reasons[1]).toHaveTextContent(reasonSentence("SOD_CONFLICT"));
    expect(row.textContent ?? "").not.toContain("NOT_IN_TEMPLATE");
    expect(row.textContent ?? "").not.toContain(RULE_ID);

    const sources = itemsOf(row, "Sources");
    expect(sources).toHaveLength(2);
    expect(sources[0]).toHaveTextContent("Fake Access Policy");
    expect(sources[0]).toHaveTextContent("v3");
    expect(sources[0]).toHaveTextContent("Fake Section › Fake Subsection");
    expect(sources[1]).toHaveTextContent("Fake Joiner Policy");
    expect(sources[1]).toHaveTextContent("v1");

    expect(at(rows, 1)).toHaveTextContent("Fake Warehouse Write");
    expect(at(rows, 1)).toHaveTextContent("Critical");
    expect(screen.queryByText(EMPTY_QUEUE)).toBeNull();
  });

  it("marks exactly the seat whose slot equals my_slot, and no other seat", async () => {
    // §1's third criterion, arranged so that only reading `my_slot` passes.
    // The caller's seat is the SECOND of the two and the RESOLVED one, so this
    // kills a row marking the first seat, a row marking every seat, and a row
    // marking the seat with no `user_id` — each of which marks seat one here.
    // The "exactly one" count, scoped to this row's `Approvers` list, is what a
    // page marking the whole chain fails; without it an approver is told all
    // four seats are theirs and has no idea which one they are filling.
    answer = () => jsonResponse({ approvals: [ROW_ONE, ROW_TWO] }, 200);
    renderQueue();

    const rows = await approvalRows();
    const seats = itemsOf(at(rows, 0), "Approvers");

    expect(seats).toHaveLength(2);
    expect(seats[0]).toHaveTextContent("Department manager");
    expect(seats[1]).toHaveTextContent("IT admin");
    expect(seats[1]).toHaveTextContent("Yours to decide");
    expect(seats[0]).not.toHaveTextContent("Yours to decide");
    expect(within(at(rows, 0)).getAllByText("Yours to decide")).toHaveLength(1);
  });
});

describe("ApprovalQueue — nothing to show, and a read that fails", () => {
  it("renders the named empty state with no list and no alert", async () => {
    // Kills a page that renders an empty list under its heading: "nothing is
    // waiting" then looks exactly like a read that returned nothing because it
    // broke. The absent alert is the other half of §1's mutual exclusion — a
    // page showing both says two contradictory things at once.
    answer = () => jsonResponse({ approvals: [] }, 200);
    renderQueue();

    expect(await screen.findByText(EMPTY_QUEUE)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Pending approvals" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  const FAILURES = [
    {
      name: "a payload the parser refuses",
      answer: (): Response => jsonResponse({ approvals: [{ request_id: REQUEST_ONE }] }, 200),
    },
    {
      name: "the proxy's own 502",
      answer: (): Response => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ] as const;

  it.each(FAILURES)(
    "$name renders the read alert, no list and no empty state",
    async (row) => {
      // Both rows kill the same wrong implementation from two directions: a
      // page answering an empty array when the read fails. It would print
      // "Nothing is waiting for your decision." over a queue full of pending
      // access — the one sentence on this page an approver acts on by closing
      // the tab — because a proxy was down. The first row additionally kills a
      // page that CASTS the body instead of running `safeParse` (the row draws
      // with blank cells and no error); the second kills one that reads the
      // body without checking the status, since a 502 envelope is valid JSON.
      // The sentence is pinned verbatim so a generic "something went wrong"
      // cannot stand in for it.
      answer = row.answer;
      renderQueue();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(READ_ALERT);
      expect(screen.queryByRole("list", { name: "Pending approvals" })).toBeNull();
      expect(screen.queryByText(EMPTY_QUEUE)).toBeNull();
    },
    10_000,
  );
});

describe("ApprovalQueue — filing a decision", () => {
  const DECISIONS = [
    {
      verb: "approve",
      field: "Comment for Fake Payroll Read",
      button: "Approve Fake Payroll Read",
      key: "comment",
      text: "Obviously fake comment.",
    },
    {
      verb: "reject",
      field: "Reason for Fake Payroll Read",
      button: "Reject Fake Payroll Read",
      key: "reason",
      text: "Obviously fake reason.",
    },
  ] as const;

  it.each(DECISIONS)(
    "$verb posts the typed body to /api/approvals/<id>/$verb",
    async (decision) => {
      // Kills a control posting to the wrong proxy — an Approve wired to
      // `.../reject` files the opposite decision, and nothing else in this file
      // would see it — and kills one posting the OTHER row's id, which is the
      // shape a handler closing over a loop variable produces. The body
      // assertion kills a control that sends the text under the wrong key or
      // sends nothing at all (the tool refuses a missing `reason`, and an
      // approver's comment is then lost from the audit trail), and the method
      // and content-type assertions kill a GET or a form-encoded post the
      // proxy forwards verbatim and agent-api refuses.
      answer = () => jsonResponse({ approvals: [ROW_ONE, ROW_TWO] }, 200);
      const user = userEvent.setup();
      renderQueue();

      const rows = await approvalRows();
      const row = at(rows, 0);
      await user.type(within(row).getByRole("textbox", { name: decision.field }), decision.text);
      await user.click(within(row).getByRole("button", { name: decision.button }));

      await waitFor(() => {
        expect(decisionCalls()).toHaveLength(1);
      });
      const call = only(decisionCalls());
      expect(call.url).toContain(`/api/approvals/${REQUEST_ONE}/${decision.verb}`);
      expect(call.url).not.toContain(REQUEST_TWO);
      expect(call.init.method).toBe("POST");
      expect(headerOf(call.init, "content-type")).toBe("application/json");
      expect(bodyOf(call.init)[decision.key]).toBe(decision.text);
    },
  );

  it("sends a 2000 character comment whole, with no maxLength on the field", async () => {
    // §1's input-validation criterion and §5's second risk. Kills a comment box
    // carrying `maxLength={2000}` or any lower bound of its own: the browser
    // would silently hand the tool a truncated comment with no error anywhere,
    // and the audit trail would carry a sentence the approver did not write.
    // The exact length is pinned at the tool's inclusive cap,
    // so a page slicing to 1999 or to any round number is red, and the missing
    // `maxlength` attribute is asserted directly because a page that truncates
    // on submit instead would pass the length assertion by accident only if it
    // did not truncate at all.
    const comment = "x".repeat(COMMENT_MAX_LENGTH);
    answer = () => jsonResponse({ approvals: [ROW_ONE, ROW_TWO] }, 200);
    const user = userEvent.setup();
    renderQueue();

    const rows = await approvalRows();
    const row = at(rows, 0);
    const field = within(row).getByRole("textbox", { name: "Comment for Fake Payroll Read" });
    expect(field).not.toHaveAttribute("maxlength");

    fireEvent.change(field, { target: { value: comment } });
    await user.click(within(row).getByRole("button", { name: "Approve Fake Payroll Read" }));

    await waitFor(() => {
      expect(decisionCalls()).toHaveLength(1);
    });
    const sent = bodyOf(only(decisionCalls()).init).comment;
    expect(sent).toBe(comment);
    expect(String(sent)).toHaveLength(COMMENT_MAX_LENGTH);
  });

  const BLANK_REASONS = [
    { name: "an empty reason", reason: "" },
    { name: "a whitespace-only reason", reason: "   " },
  ] as const;

  it.each(BLANK_REASONS)("$name makes no call at all", async (row) => {
    // §1's floor, and the only refusal the page can answer without spending a
    // round trip and a rate-limit slot. Kills a Reject that posts whatever is
    // in the box: the tool would answer `VALIDATION`, the approver would read
    // an error for something the page knew was wrong before it asked, and the
    // rate limiter would have been charged for it. The whitespace row is the
    // load-bearing one — a `reason === ""` check passes it through, so only a
    // page that TRIMS is green — and the assertion is the absence of a call
    // rather than the presence of a message, because a page that both warns
    // and posts has not satisfied the criterion.
    answer = () => jsonResponse({ approvals: [ROW_ONE, ROW_TWO] }, 200);
    const user = userEvent.setup();
    renderQueue();

    const rows = await approvalRows();
    const target = at(rows, 0);
    if (row.reason !== "") {
      await user.type(
        within(target).getByRole("textbox", { name: "Reason for Fake Payroll Read" }),
        row.reason,
      );
    }
    await user.click(within(target).getByRole("button", { name: "Reject Fake Payroll Read" }));

    expect(decisionCalls()).toHaveLength(0);
    expect(readCallCount()).toBe(1);
  });

  it("disables both controls of the deciding row while its decision is in flight, and no other row's", async () => {
    // §5's first risk, and the case says plainly what it is: the guarantee
    // against a double-filed decision is the server-derived idempotency key
    // (23c decision B), and these disabled controls are a courtesy on top of
    // it. What this kills is a page holding ONE in-flight flag for the whole
    // queue — every Approve in the list would grey out while one row is being
    // decided, so an approver with ten items waits for each — and a page with
    // no flag at all, where the Reject beside an in-flight Approve is still
    // live and files the opposite decision on the same request. The second
    // row's two enabled controls are the half that a global flag fails.
    answer = () => jsonResponse({ approvals: [ROW_ONE, ROW_TWO] }, 200);
    let release: (value: Response) => void = () => {};
    decisionAnswer = () =>
      new Promise<Response>((resolve) => {
        release = resolve;
      });
    const user = userEvent.setup();
    renderQueue();

    const rows = await approvalRows();
    const deciding = at(rows, 0);
    const other = at(rows, 1);
    await user.click(within(deciding).getByRole("button", { name: "Approve Fake Payroll Read" }));

    await waitFor(() => {
      expect(
        within(deciding).getByRole("button", { name: "Approve Fake Payroll Read" }),
      ).toBeDisabled();
    });
    expect(
      within(deciding).getByRole("button", { name: "Reject Fake Payroll Read" }),
    ).toBeDisabled();
    expect(
      within(other).getByRole("button", { name: "Approve Fake Warehouse Write" }),
    ).toBeEnabled();
    expect(
      within(other).getByRole("button", { name: "Reject Fake Warehouse Write" }),
    ).toBeEnabled();

    release(jsonResponse({ status: "approved" }, 200));
    await waitFor(() => {
      expect(
        within(deciding).getByRole("button", { name: "Approve Fake Payroll Read" }),
      ).toBeEnabled();
    });
  });

  it("refetches the queue after a successful decision and removes nothing before the answer", async () => {
    // §1's invalidation criterion and decision E, both halves. The second read
    // is held open, so the moment AFTER the decision succeeded and BEFORE the
    // refetch answered is observable: a page removing the row optimistically
    // shows a decision as done that the tool may have answered `failed` with a
    // `failure_code`, or that left a chain with seats still open. Kills that,
    // and kills a mutation with no `invalidateQueries(["me", "approvals"])` at
    // all — the row would sit in the queue, still offering Approve, until the
    // approver reloaded by hand. Arithmetic: reads = 2, the
    // first answering two rows and the second one row; decision calls = 1;
    // rendered rows go 2 -> 2 (while the refetch is in flight) -> 1.
    let releaseRead: (value: Response) => void = () => {};
    let reads = 0;
    answer = () => {
      reads += 1;
      if (reads === 1) {
        return jsonResponse({ approvals: [ROW_ONE, ROW_TWO] }, 200);
      }
      return new Promise<Response>((resolve) => {
        releaseRead = resolve;
      });
    };
    const user = userEvent.setup();
    renderQueue();

    const rows = await approvalRows();
    expect(rows).toHaveLength(2);
    await user.type(
      within(at(rows, 0)).getByRole("textbox", { name: "Comment for Fake Payroll Read" }),
      "Obviously fake comment.",
    );
    await user.click(
      within(at(rows, 0)).getByRole("button", { name: "Approve Fake Payroll Read" }),
    );

    await waitFor(() => {
      expect(readCallCount()).toBe(2);
    });
    expect(await approvalRows()).toHaveLength(2);

    releaseRead(jsonResponse({ approvals: [ROW_TWO] }, 200));
    await waitFor(async () => {
      expect(await approvalRows()).toHaveLength(1);
    });
    expect(at(await approvalRows(), 0)).toHaveTextContent("Fake Warehouse Write");
    expect(decisionCalls()).toHaveLength(1);
  });
});

describe("ApprovalQueue — a refused decision", () => {
  const REFUSALS = [
    { name: "FORBIDDEN", status: 403, code: "FORBIDDEN", sentence: FAILURE_MESSAGES.FORBIDDEN },
    {
      name: "TOOL_DISABLED",
      status: 503,
      code: "TOOL_DISABLED",
      sentence: FAILURE_MESSAGES.TOOL_DISABLED,
    },
    {
      name: "VALIDATION",
      status: 400,
      code: "VALIDATION",
      sentence: DECISION_VALIDATION_MESSAGE,
    },
  ] as const;

  it.each(REFUSALS)(
    "$name renders its sentence in that row's alert and leaves the queue intact",
    async (refusal) => {
      // Kills three things at once. A page rendering the CODE at the approver
      // ("TOOL_DISABLED" means nothing to the person reading it) — the
      // sentences are read out of `FAILURE_MESSAGES` rather than retyped, so a
      // page inventing its own wording for `FORBIDDEN` is red without widening
      // 22b's pinned map. A page whose mutation error clears the rows the read
      // answered (§5's fourth risk): the count assertion after the refusal is
      // what catches a queue emptied by a 403. And, on the VALIDATION row, a
      // page that used the SHARED sentence for it — "That message could not be
      // sent." is about a chat message and says nothing true about a comment
      // the tool refused, which is the whole of decision B; that row asserts
      // the override's value AND the absence of the shared string, so reusing
      // the map is red in both directions.
      answer = () => jsonResponse({ approvals: [ROW_ONE, ROW_TWO] }, 200);
      decisionAnswer = () => jsonResponse({ code: refusal.code }, refusal.status);
      const user = userEvent.setup();
      renderQueue();

      const rows = await approvalRows();
      const row = at(rows, 0);
      await user.type(
        within(row).getByRole("textbox", { name: "Comment for Fake Payroll Read" }),
        "Obviously fake comment.",
      );
      await user.click(within(row).getByRole("button", { name: "Approve Fake Payroll Read" }));

      const alert = await within(row).findByRole("alert");
      expect(alert).toHaveTextContent(refusal.sentence);
      expect(await approvalRows()).toHaveLength(2);
      expect(screen.queryByText(READ_ALERT)).toBeNull();
      expect(screen.queryByText(EMPTY_QUEUE)).toBeNull();
      if (refusal.code === "VALIDATION") {
        expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.VALIDATION);
      }
    },
  );

  it("pins the VALIDATION override sentence exactly", async () => {
    // a project rule on the one string this plan introduces. Kills an
    // implementation that exports the constant but fills it with the shared
    // map's sentence, or with a reworded variant: the parametrised row above
    // reads the constant, so it stays green for any value the module chooses,
    // and only this literal fixes it. Awaited nothing: the constant is the
    // whole subject.
    expect(DECISION_VALIDATION_MESSAGE).toBe(
      "That decision could not be filed. Check the comment and try again.",
    );
    expect(DECISION_VALIDATION_MESSAGE).not.toBe(FAILURE_MESSAGES.VALIDATION);
  });
});

describe("ApprovalQueue — an expired session", () => {
  it("follows the proxy's redirect to /login instead of rendering the queue", async () => {
    // §1's unhappy path at the READ, and the reason it is `response.redirected`
    // and never a status (22b decision E, a project rule): the browser has
    // ALREADY followed the proxy's 307, so what arrives is a 200 carrying the
    // login page. Kills a fetcher checking `response.ok` or `response.status
    // === 401` — both see a successful response, so the parser refuses the HTML
    // and the approver sits on `/approvals` reading a read error instead of
    // being asked to sign in again.
    answer = redirectedResponse;
    renderQueue();

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(screen.queryByRole("list", { name: "Pending approvals" })).toBeNull();
  });

  it("follows the redirect at the decision too, not only at the read", async () => {
    // The half a fetcher fixed in one place misses. A session that expires
    // BETWEEN the read and the click is the common case — the queue is open on
    // a screen for minutes — and a mutation that ignores `redirected` parses
    // the login HTML, throws, and shows the approver a refusal sentence for a
    // decision that was never filed. Kills exactly that: the read here
    // succeeds, so only the decision path can make the assignment happen, and
    // no read-side redirect handling can stand in for it.
    answer = () => jsonResponse({ approvals: [ROW_ONE, ROW_TWO] }, 200);
    decisionAnswer = redirectedResponse;
    const user = userEvent.setup();
    renderQueue();

    const rows = await approvalRows();
    await user.click(
      within(at(rows, 0)).getByRole("button", { name: "Approve Fake Payroll Read" }),
    );

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
  });
});

/**
 * An earlier change §6, added to 23d's file and rewriting none of it (§8): the three
 * cases that hold the QUEUE read still, after its four private lines become
 * `readWire("/api/me/approvals", pendingApprovalsSchema)`. The two POSTs are
 * not reads and are untouched by that plan, which is why no case here stages a
 * decision: `DecisionRefusedError` and its per-code sentences stay exactly as
 * the cases above pin them.
 */
describe("ApprovalQueue — the queue read through the shared wire reader", () => {
  const ORIGIN = "http://localhost:3000";

  /** The single queue read, as a URL a case can assert field by field. */
  function onlyQueueUrl(): URL {
    const urls = fetchStub.mock.calls
      .map(([input]) => urlOf(input))
      .filter((url) => url.includes("/api/me/approvals"));
    const first = urls[0];
    if (urls.length !== 1 || first === undefined) {
      throw new Error(`expected exactly one queue read, found ${urls.length}`);
    }
    return new URL(first, ORIGIN);
  }

  it("a 403 FORBIDDEN on the queue renders the read alert, not the code's sentence", async () => {
    // Decision B, and the page where it is easiest to get wrong: this file
    // ALREADY renders `FAILURE_MESSAGES` sentences for a refused DECISION, so a
    // migration that reuses that dressing for the READ is one import away. Kills
    // exactly that — "You are not allowed to do that." in the region alert would
    // tell an approver they may not approve, when what failed is the listing —
    // and kills a `WireRefusedError` escaping the error branch, which leaves the
    // page on its loading state with neither alert nor queue. Neither of the two
    // rows above can decide it: a refused payload and a 502 both arrive as plain
    // errors through the migrated reader, so only a typed body reaches the new
    // class.
    answer = () => jsonResponse({ code: "FORBIDDEN" }, 403);
    renderQueue();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(READ_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.FORBIDDEN);
    expect(screen.queryByRole("list", { name: "Pending approvals" })).toBeNull();
    expect(screen.queryByText(EMPTY_QUEUE)).toBeNull();
  }, 10_000);

  it("reads /api/me/approvals with an empty query string", async () => {
    // The path and the query byte for byte, as an exact `pathname` and an exact
    // `search`. Kills a migration that moves the queue read onto
    // `/api/approvals` — the POST prefix this same file uses, one character away
    // and a route that takes a request id — and kills one that gains a filter
    // (`?slot=`, `?status=pending`) while being rewritten: which items wait on
    // this approver is decided behind the MCP boundary off the token, and a page
    // naming a seat or a principal is a project rule in the browser. Arithmetic
    //: the staged answer is the default empty 200, so queue
    // reads = 1 on mount and decision calls = 0, and the empty state is what
    // says the read settled before the count is taken.
    answer = () => jsonResponse({ approvals: [] }, 200);
    renderQueue();

    await screen.findByText(EMPTY_QUEUE);
    const url = onlyQueueUrl();
    expect(url.pathname).toBe("/api/me/approvals");
    expect(url.search).toBe("");
    expect(decisionCalls()).toHaveLength(0);
  });

  it("a bodyless 500 on the queue renders the same read alert", async () => {
    // §5's second risk on this page: `readWire` reads the body of every non-ok
    // response looking for a typed code, and `fetchApprovals` reads it for none.
    // Kills a refusal branch awaiting an unguarded `json()` (an outage would
    // then surface as a parser message, or as no alert at all), and kills one
    // classifying an unreadable body as a refusal carrying `undefined`, which
    // `failureMessage` answers with the default sentence instead of this page's.
    answer = () => new Response(null, { status: 500 });
    renderQueue();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(READ_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.default);
    expect(screen.queryByRole("list", { name: "Pending approvals" })).toBeNull();
    expect(screen.queryByText(EMPTY_QUEUE)).toBeNull();
  }, 10_000);
});
