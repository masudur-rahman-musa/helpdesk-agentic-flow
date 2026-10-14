import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationBell } from "@/features/notifications/components/notification-bell";
import {
  NOTIFICATION_VALIDATION_MESSAGE,
  NotificationList,
} from "@/features/notifications/components/notification-list";
import { QueryProvider } from "@/components/layout/query-provider";
import { FAILURE_MESSAGES } from "@/features/chat/errors";
import { notificationKindLabel } from "@/features/notifications/labels";

/**
 * An earlier change §6, `notifications-page.test.tsx`: the `/notifications` list, its
 * `Unread only` filter and the per-row read stamp, against a stubbed `fetch`.
 * jsdom, no agent-api, no backing service. (No basename collision in the tree,
 * a project rule.)
 *
 * The unit under test is `NotificationList` from
 * `web/src/features/notifications/components/notification-list.tsx`, taking NO props and
 * doing its own read and its own mutations through TanStack Query (§2, 23d
 * decision A), so `app/notifications/page.tsx` stays a thin wrapper no case
 * here renders.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23gc's Deviations):
 * - `NOTIFICATION_VALIDATION_MESSAGE` is imported from the COMPONENT module,
 *   not from `lib/`. §6 names the constant and not its module; 23f's sibling
 *   override `REVIEW_VALIDATION_MESSAGE` lives in
 *   `components/reviews/review-queue.tsx`, and decision G's whole point is
 *   that the override belongs to this call site rather than to the shared map.
 * - `Unread only` is a CHECKBOX — located as `role="checkbox"` named `Unread
 *   only`. §1 fixes the name and the parameter each state sends; it does not
 *   fix the widget, and a switch-styled button would not be findable by the
 *   same query.
 * - a refused STAMP's sentence is a `role="alert"` inside that ROW's listitem;
 *   a failed page READ's sentence is the region's own alert. The two are
 *   located differently on purpose (§1's last two unhappy paths).
 * - the rows are the DIRECT listitem children of their list (`rowsOf`),
 *   because `getAllByRole("listitem")` inside a list also returns every nested
 *   list's items.
 * - dates are the ISO prefix (`2026-06-01`), never `toLocaleDateString`, which
 *   under jsdom answers `6/1/2026` — a different day in half the world.
 *
 * The wire shape is 23gb's as BUILT and every fixture here parses under
 * `web/src/features/notifications/schema.ts`: the eight-field row inside
 * `{ notifications: [...], unread_count: N }`, the stamp's four-field answer,
 * and a failed proxy answering `502 {"code": "UPSTREAM_UNAVAILABLE"}`
 * (`proxyAgentApi`). `notification_id` is a bare `z.string()` there, which is
 * why the escaping row below is a payload the parser accepts.
 *
 * The kind labels are read through `lib/notification-labels.ts` rather than
 * retyped, the way `reviews-page.test.tsx` reads `reviewFlagLabel`: §6 fixes no
 * label text, and `notification-labels.test.ts` is what pins each one as a
 * label rather than as the raw wire code.
 */

const EMPTY_LIST = "You have no notifications.";
const PAGE_ALERT = "Your notifications could not be read. Try again in a moment.";
const LOGIN_URL = "http://localhost:3000/login";

const UNREAD_ONE = "11111111-1111-5111-8111-111111111111";
/** A id carrying a path separator: `notification_id` is a bare `z.string()` on
 * the wire, so this is a payload 23gb's parser accepts and the one value that
 * can tell `encodeURIComponent` apart from raw interpolation. */
const UNREAD_TWO = "22222222-2222-5222-8222-222222222222/edge";
const READ_THREE = "33333333-3333-5333-8333-333333333333";
const UNREAD_FOUR = "44444444-4444-5444-8444-444444444444";

/** An UNREAD notification about an expired grant: both subject fields set. */
const ROW_ONE: Record<string, unknown> = {
  notification_id: UNREAD_ONE,
  kind: "grant_expired",
  title: "Fake Payroll Read has expired",
  body: "Your access to Fake Payroll ended on the first.",
  subject_type: "grant",
  subject_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
  created_at: "2026-06-01T09:00:00Z",
  read_at: null,
};

/** A second UNREAD notification, so "this row's control and no other's" is
 * assertable: its title differs, so its control name does too. Both subject
 * fields are null — a notification about nothing in particular. */
const ROW_TWO: Record<string, unknown> = {
  notification_id: UNREAD_TWO,
  kind: "review_overdue",
  title: "Fake Finance review is overdue",
  body: "The review of Fake Finance passed its due date.",
  subject_type: null,
  subject_id: null,
  created_at: "2026-06-02T07:15:00Z",
  read_at: null,
};

/** An already READ notification: §1 says it carries no control at all. */
const ROW_THREE: Record<string, unknown> = {
  notification_id: READ_THREE,
  kind: "grant_expiring",
  title: "Fake Warehouse Write ends soon",
  body: "Your access to Fake Warehouse ends in three days.",
  subject_type: "grant",
  subject_id: "bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb",
  created_at: "2026-05-28T11:45:00Z",
  read_at: "2026-06-02T08:30:00Z",
};

/**
 * The only row the FILTERED read answers, and one the unfiltered read never
 * sent. A page filtering its cached list in place cannot produce it, which is
 * what makes "toggling refetches" observable rather than inferred.
 */
const ROW_FOUR: Record<string, unknown> = {
  notification_id: UNREAD_FOUR,
  kind: "review_escalated",
  title: "Fake Archive review was escalated",
  body: "The review of Fake Archive went to the next approver.",
  subject_type: null,
  subject_id: null,
  created_at: "2026-06-03T05:00:00Z",
  read_at: null,
};

/** Three rows, two of them unread: `unread_count` is 2 by hand-count. */
const UNFILTERED_PAGE: Record<string, unknown> = {
  notifications: [ROW_ONE, ROW_TWO, ROW_THREE],
  unread_count: 2,
};

/** One row, unread: `unread_count` is 1. */
const FILTERED_PAGE: Record<string, unknown> = {
  notifications: [ROW_FOUR],
  unread_count: 1,
};

/** `mark_notification_read`'s answer for the first row. Its `read_at` differs
 * from the one the refetch below answers on purpose: §1 forbids writing this
 * value into the list by hand. */
const STAMP_ANSWER: Record<string, unknown> = {
  notification_id: UNREAD_ONE,
  kind: "grant_expired",
  title: "Fake Payroll Read has expired",
  read_at: "2026-06-05T10:00:00Z",
};

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

type Answer = () => Response | Promise<Response>;

let pageAnswer: Answer = () => jsonResponse({ notifications: [], unread_count: 0 }, 200);
let filteredAnswer: Answer = () => jsonResponse(FILTERED_PAGE, 200);
let bellAnswer: Answer = () => jsonResponse({ notifications: [], unread_count: 0 }, 200);
let stampAnswer: Answer = () => jsonResponse(STAMP_ANSWER, 200);

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

/** The bell's read is the one carrying `limit=1` (decision C); the page sends
 * no `limit` at all (decision E), so the two are told apart by that key. */
function isBellRead(url: string): boolean {
  return url.includes("/api/me/notifications") && url.includes("limit=");
}

function isPageRead(url: string): boolean {
  return url.includes("/api/me/notifications") && !url.includes("limit=");
}

function isStamp(url: string): boolean {
  return url.includes("/api/notifications/") && url.endsWith("/read");
}

function renderList(): void {
  render(
    <QueryProvider>
      <NotificationList />
    </QueryProvider>,
  );
}

/** The page and the bell under ONE client, which is how the shared
 * `["notifications"]` prefix is observable at all (§6, decision C). */
function renderListWithBell(): void {
  render(
    <QueryProvider>
      <NotificationBell />
      <NotificationList />
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
  return await screen.findByRole("region", { name: "Notifications" });
}

async function notificationRows(): Promise<HTMLElement[]> {
  return rowsOf(await within(await region()).findByRole("list", { name: "Your notifications" }));
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

function pageCalls(): Call[] {
  return callsMatching(isPageRead);
}

function bellCalls(): Call[] {
  return callsMatching(isBellRead);
}

function stampCalls(): Call[] {
  return callsMatching(isStamp);
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

function stageThreeNotifications(): void {
  pageAnswer = () => jsonResponse(UNFILTERED_PAGE, 200);
}

function rowTitled(rows: HTMLElement[], title: string): HTMLElement {
  const found = rows.find((row) => (row.textContent ?? "").includes(title));
  if (found === undefined) {
    throw new Error(`no row mentioned ${title}`);
  }
  return found;
}

async function toggleUnreadOnly(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(await screen.findByRole("checkbox", { name: "Unread only" }));
}

beforeEach(() => {
  pageAnswer = () => jsonResponse({ notifications: [], unread_count: 0 }, 200);
  filteredAnswer = () => jsonResponse(FILTERED_PAGE, 200);
  bellAnswer = () => jsonResponse({ notifications: [ROW_ONE], unread_count: 2 }, 200);
  stampAnswer = () => jsonResponse(STAMP_ANSWER, 200);
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (isBellRead(url)) {
      return await bellAnswer();
    }
    if (isPageRead(url)) {
      return url.includes("unread_only") ? await filteredAnswer() : await pageAnswer();
    }
    if (isStamp(url)) {
      return await stampAnswer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", {
    ...window.location,
    href: "http://localhost:3000/notifications",
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("NotificationList — the rows", () => {
  it("renders one row per notification with its kind label, title, body, ISO created date and read state", async () => {
    // §1's first two criteria, one assertion per field. Kills a list rendering
    // only `notifications[0]`; kills a row printing the wire `kind` at a person
    // (`grant_expired` explains nothing and is the exact drift
    // `lib/notification-labels.ts` exists to stop, so the raw code's ABSENCE is
    // asserted beside the label's presence); kills a row that drops the BODY,
    // which is the only part saying what actually happened; and the ISO
    // assertion kills `toLocaleDateString`, which under jsdom answers
    // `6/1/2026`. The read-state half is what separates the two unread rows
    // from the read one on a list that is otherwise identical, and the read
    // row's `Read 2026-06-02` kills a page printing only the word.
    stageThreeNotifications();
    renderList();

    const rows = await notificationRows();
    expect(rows).toHaveLength(3);

    const first = at(rows, 0);
    expect(first).toHaveTextContent(notificationKindLabel("grant_expired"));
    expect(first).toHaveTextContent("Fake Payroll Read has expired");
    expect(first).toHaveTextContent("Your access to Fake Payroll ended on the first.");
    expect(first).toHaveTextContent("2026-06-01");
    expect(first).toHaveTextContent("Unread");
    expect(first.textContent ?? "").not.toContain("grant_expired");
    expect(first.textContent ?? "").not.toContain("6/1/2026");

    const second = at(rows, 1);
    expect(second).toHaveTextContent(notificationKindLabel("review_overdue"));
    expect(second).toHaveTextContent("Fake Finance review is overdue");
    expect(second).toHaveTextContent("2026-06-02");
    expect(second.textContent ?? "").not.toContain("review_overdue");

    const third = at(rows, 2);
    expect(third).toHaveTextContent(notificationKindLabel("grant_expiring"));
    expect(third).toHaveTextContent("2026-05-28");
    expect(third).toHaveTextContent("Read 2026-06-02");
    expect(third.textContent ?? "").not.toContain("8/30");
    expect(screen.queryByText(EMPTY_LIST)).toBeNull();
  });

  it("gives every unread row a Mark read control and the read row none", async () => {
    // §1's fifth criterion, both halves on the rows that carry them. Kills a
    // list offering `Mark read` on a row the tool has already stamped: the
    // second stamp answers the STORED timestamp (23ga decision A), so what the
    // person gets for clicking is a no-op on a row that was never theirs to
    // act on, and the bell's count never moves. The per-row name is asserted
    // WITH the title (§5's collision risk) — a control named `Mark read` alone
    // is fifty identical accessible names on a full page, and no case could
    // then say which row was clicked.
    stageThreeNotifications();
    renderList();

    const rows = await notificationRows();
    expect(
      within(rowTitled(rows, "Fake Payroll Read has expired")).getByRole("button", {
        name: "Mark read Fake Payroll Read has expired",
      }),
    ).toBeInTheDocument();
    expect(
      within(rowTitled(rows, "Fake Finance review is overdue")).getByRole("button", {
        name: "Mark read Fake Finance review is overdue",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Mark read Fake Warehouse Write ends soon" }),
    ).toBeNull();
  });
});

describe("NotificationList — the Unread only filter", () => {
  it("paints the unfiltered list first, sending no unread_only and no limit", async () => {
    // §1's third criterion, the half that decides what a person's FIRST view of
    // the route is. Kills a page that opens filtered — the read rows then
    // vanish the moment they are stamped and there is no way back to "everything
    // I have" without knowing the control exists — and kills one sending
    // `unread_only=false` or `unread_only=`, neither of which is a bool the
    // tool accepts (decision E is the ABSENCE of the key, not a false value).
    // The `limit` half is decision E's other clause: nothing on this surface
    // pages, so the tool's own default is what the list shows.
    stageThreeNotifications();
    renderList();

    await notificationRows();
    const call = only(pageCalls(), "page read");
    expect(call.url).toContain("/api/me/notifications");
    expect(call.url).not.toContain("unread_only");
    expect(call.url).not.toContain("limit");
  });

  it("turning Unread only on refetches with unread_only=true", async () => {
    // Two things at once. That a SECOND read happens at all is what pins the
    // query key carrying the choice — a key of `["notifications", "page"]`
    // alone answers the cached unfiltered page and the click does nothing for
    // thirty seconds, with no request to see. And the row that appears pins
    // that the list is the REFETCH's answer rather than a client-side filter
    // over what it already held: `Fake Archive review was escalated` was in no
    // earlier payload, so a page filtering in place cannot render it, and a
    // page filtering in place would also keep showing the read row. Arithmetic
    //: page reads = 2, the first with no parameter, the
    // second `unread_only=true`; rows 3 -> 1; no bell is mounted, so bell
    // reads = 0.
    stageThreeNotifications();
    const user = userEvent.setup();
    renderList();

    expect(await notificationRows()).toHaveLength(3);
    await toggleUnreadOnly(user);

    await waitFor(() => {
      expect(pageCalls()).toHaveLength(2);
    });
    expect(lastOf(pageCalls()).url).toContain("unread_only=true");
    expect(lastOf(pageCalls()).url).not.toContain("limit");
    await waitFor(async () => {
      expect(await notificationRows()).toHaveLength(1);
    });
    expect(at(await notificationRows(), 0)).toHaveTextContent("Fake Archive review was escalated");
    expect(screen.queryByText("Fake Warehouse Write ends soon")).toBeNull();
    expect(bellCalls()).toHaveLength(0);
  });
});

describe("NotificationList — stamping a row read", () => {
  it("posts to that row's read proxy with no body and the id escaped", async () => {
    // Kills a page posting the OTHER row's id — the shape a handler closing
    // over a loop variable produces, and one that clears a notification the
    // person never opened — and kills a page interpolating the id raw: a
    // `notification_id` is a bare `z.string()` on the wire, so a value carrying
    // a path separator would reach a different route entirely. The absent body
    // and absent content type are 23gb decision B: the proxy forwards a
    // bodyless POST, and a client-minted idempotency key in a body would be
    // both ignored and a client-supplied string this page has no business
    // choosing (§1's input-validation criterion).
    stageThreeNotifications();
    const user = userEvent.setup();
    renderList();

    const rows = await notificationRows();
    await user.click(
      within(rowTitled(rows, "Fake Finance review is overdue")).getByRole("button", {
        name: "Mark read Fake Finance review is overdue",
      }),
    );

    await waitFor(() => {
      expect(stampCalls()).toHaveLength(1);
    });
    const call = only(stampCalls(), "stamp");
    expect(call.url).toContain(`/api/notifications/${encodeURIComponent(UNREAD_TWO)}/read`);
    expect(call.url).not.toContain(`/api/notifications/${UNREAD_TWO}/read`);
    expect(call.url).not.toContain(UNREAD_ONE);
    expect(call.init.method).toBe("POST");
    expect(call.init.body).toBeUndefined();
    expect(headerOf(call.init, "content-type")).toBeNull();
  });

  it("refetches both the list and the bell through the notifications prefix", async () => {
    // §5's second risk, which is the one a page-key-only invalidation ships
    // silently: the two queries are separate (decision C), so a stamp
    // invalidating the page's own key leaves the bell in the header of every
    // screen showing a count that includes the row the person just cleared, and
    // only a reload fixes it. Kills that, kills a mutation with no
    // invalidation at all (the row would keep its control), and kills a page
    // that writes the stamp's returned `read_at` into the list by hand: the
    // rendered date is the REFETCH's `2026-06-03` and the stamp answered
    // `2026-06-05`, so a hand-written cache update shows a date the list read
    // never returned. Arithmetic: page reads 1 -> 2, bell
    // reads 1 -> 2, stamp calls = 1; unread_count 2 -> 1, so the bell reads
    // `Notifications, 1 unread` at the end.
    let pageReads = 0;
    let bellReads = 0;
    pageAnswer = () => {
      pageReads += 1;
      const first = pageReads === 1 ? ROW_ONE : { ...ROW_ONE, read_at: "2026-06-03T06:00:00Z" };
      return jsonResponse(
        {
          notifications: [first, ROW_TWO, ROW_THREE],
          unread_count: pageReads === 1 ? 2 : 1,
        },
        200,
      );
    };
    bellAnswer = () => {
      bellReads += 1;
      return jsonResponse({ notifications: [ROW_ONE], unread_count: bellReads === 1 ? 2 : 1 }, 200);
    };
    const user = userEvent.setup();
    renderListWithBell();

    expect(
      await screen.findByRole("link", { name: "Notifications, 2 unread" }),
    ).toBeInTheDocument();
    const rows = await notificationRows();
    await user.click(
      within(rowTitled(rows, "Fake Payroll Read has expired")).getByRole("button", {
        name: "Mark read Fake Payroll Read has expired",
      }),
    );

    await waitFor(() => {
      expect(pageCalls()).toHaveLength(2);
    });
    await waitFor(() => {
      expect(bellCalls()).toHaveLength(2);
    });
    await waitFor(async () => {
      expect(
        rowTitled(await notificationRows(), "Fake Payroll Read has expired"),
      ).toHaveTextContent("Read 2026-06-03");
    });
    expect(
      await screen.findByRole("link", { name: "Notifications, 1 unread" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Mark read Fake Payroll Read has expired" }),
    ).toBeNull();
    expect(screen.queryByText("2026-06-05")).toBeNull();
    expect(stampCalls()).toHaveLength(1);
  });

  it("disables the stamping row's control while it is in flight, and no other row's", async () => {
    // §1's sixth criterion. Kills a page holding ONE in-flight flag for the
    // whole list — every control greys out while one row is being stamped, so a
    // person clearing a page of fifty waits for each — and the enabled control
    // on the OTHER unread row is the half only a per-row flag satisfies. The
    // re-enable at the end is what kills a flag that is set and never cleared,
    // which would leave the rest of the list unclickable after one stamp.
    stageThreeNotifications();
    let release: (value: Response) => void = () => {};
    stampAnswer = () =>
      new Promise<Response>((resolve) => {
        release = resolve;
      });
    const user = userEvent.setup();
    renderList();

    const rows = await notificationRows();
    const stamping = rowTitled(rows, "Fake Payroll Read has expired");
    const other = rowTitled(rows, "Fake Finance review is overdue");
    await user.click(
      within(stamping).getByRole("button", { name: "Mark read Fake Payroll Read has expired" }),
    );

    await waitFor(() => {
      expect(
        within(stamping).getByRole("button", { name: "Mark read Fake Payroll Read has expired" }),
      ).toBeDisabled();
    });
    expect(
      within(other).getByRole("button", { name: "Mark read Fake Finance review is overdue" }),
    ).toBeEnabled();

    release(jsonResponse(STAMP_ANSWER, 200));
    await waitFor(() => {
      expect(
        within(other).getByRole("button", { name: "Mark read Fake Finance review is overdue" }),
      ).toBeEnabled();
    });
  });
});

describe("NotificationList — a refused stamp", () => {
  const REFUSALS = [
    { name: "FORBIDDEN", status: 403, code: "FORBIDDEN", sentence: FAILURE_MESSAGES.FORBIDDEN },
    {
      name: "TOOL_DISABLED",
      status: 503,
      code: "TOOL_DISABLED",
      sentence: FAILURE_MESSAGES.TOOL_DISABLED,
    },
    { name: "NOT_FOUND", status: 404, code: "NOT_FOUND", sentence: FAILURE_MESSAGES.NOT_FOUND },
    {
      name: "VALIDATION",
      status: 400,
      code: "VALIDATION",
      sentence: NOTIFICATION_VALIDATION_MESSAGE,
    },
  ] as const;

  it.each(REFUSALS)(
    "$name renders its sentence in that row's alert and empties the list",
    async (refusal) => {
      // Four kills in one case. A page rendering the CODE at the person
      // ("TOOL_DISABLED" means nothing to whoever reads it) — the three shared
      // sentences are READ from `FAILURE_MESSAGES` rather than retyped, so a page
      // inventing its own wording for `FORBIDDEN` is red without widening 22b's
      // pinned map. A page whose mutation error clears the rows the read
      // answered: the count after the refusal is what catches a list emptied by a
      // 403, and §1 says none of the four may empty it. On the VALIDATION row, a
      // page that used the SHARED sentence — "That message could not be sent." is
      // about a chat message and says nothing true about a refused stamp, which
      // is the whole of decision G; that row asserts the override AND the absence
      // of the shared string, so reusing the map is red in both directions. And
      // the alert is located INSIDE the row, so a page showing one refusal banner
      // for the whole list (nobody can tell which of fifty rows was refused)
      // fails here too.
      stageThreeNotifications();
      stampAnswer = () => jsonResponse({ code: refusal.code }, refusal.status);
      const user = userEvent.setup();
      renderList();

      const row = rowTitled(await notificationRows(), "Fake Payroll Read has expired");
      await user.click(
        within(row).getByRole("button", { name: "Mark read Fake Payroll Read has expired" }),
      );

      const alert = await within(row).findByRole("alert");
      expect(alert).toHaveTextContent(refusal.sentence);
      expect(await notificationRows()).toHaveLength(3);
      expect(screen.queryByText(PAGE_ALERT)).toBeNull();
      expect(screen.queryByText(EMPTY_LIST)).toBeNull();
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
    expect(NOTIFICATION_VALIDATION_MESSAGE).toBe(
      "That notification could not be marked read. Reload the page and try again.",
    );
    expect(NOTIFICATION_VALIDATION_MESSAGE).not.toBe(FAILURE_MESSAGES.VALIDATION);
  });
});

describe("NotificationList — nothing to show, and a read that fails", () => {
  it("renders the named empty state with no list and no alert", async () => {
    // Kills a page rendering an empty list under its heading: "you have no
    // notifications" then looks exactly like a read that returned nothing
    // because it broke. The absent alert is one half of §1's mutual exclusion —
    // a page showing both says two contradictory things at once.
    pageAnswer = () => jsonResponse({ notifications: [], unread_count: 0 }, 200);
    renderList();

    expect(await screen.findByText(EMPTY_LIST)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Your notifications" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  const READ_FAILURES = [
    {
      name: "a payload the parser refuses",
      answer: (): Response => jsonResponse({ notifications: [{ kind: "grant_expired" }] }, 200),
    },
    {
      name: "the proxy's own 502",
      answer: (): Response => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ] as const;

  it.each(READ_FAILURES)(
    "$name renders the page alert, no list and no empty state",
    async (row) => {
      // Both rows kill one wrong implementation from two directions: a fetcher
      // answering an empty list when the read fails. It would print "You have
      // no notifications." over a broken proxy — the one sentence on this page
      // a person acts on by closing the tab, believing an expiring grant was
      // never announced — which is §5's fourth risk and the other half of §1's
      // mutual exclusion. The first row additionally kills a page that CASTS
      // the body instead of running `safeParse` (the rows draw with blank cells
      // and no error, and `unread_count` is missing entirely); the second kills
      // one reading the body without checking the status, since a 502 envelope
      // is valid JSON. The sentence is pinned verbatim so a generic "something
      // went wrong" cannot stand in for it.
      pageAnswer = row.answer;
      renderList();

      const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
      expect(alert).toHaveTextContent(PAGE_ALERT);
      expect(screen.queryByRole("list", { name: "Your notifications" })).toBeNull();
      expect(screen.queryByText(EMPTY_LIST)).toBeNull();
    },
    10_000,
  );
});

describe("NotificationList — an expired session", () => {
  it("follows the proxy's redirect to /login instead of rendering the list", async () => {
    // §1's last unhappy path at the page read, and the reason it is
    // `response.redirected` and never a status (22b decision E, standing rule
    // 59): the browser has ALREADY followed the proxy's redirect, so what
    // arrives is a 200 carrying the login page. Kills a fetcher checking
    // `response.ok` or `response.status === 401` — both see a successful
    // response, the parser then refuses the HTML, and the person sits on
    // `/notifications` reading a read error instead of being asked to sign in.
    pageAnswer = redirectedResponse;
    renderList();

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(screen.queryByRole("list", { name: "Your notifications" })).toBeNull();
  });

  it("follows the redirect at the stamp too", async () => {
    // The half a fetcher fixed in one place misses, at the second of this
    // page's two call sites, and the likelier one: a notifications list sits
    // open on a screen for many minutes before anyone clicks. A mutation
    // ignoring `redirected` parses the login HTML, throws, and shows a refusal
    // sentence for a stamp that was never filed — the person believes they
    // cleared a row they did not, and the bell disagrees with the page. The
    // page read SUCCEEDS here, so only the stamp path can satisfy this.
    stageThreeNotifications();
    stampAnswer = redirectedResponse;
    const user = userEvent.setup();
    renderList();

    const row = rowTitled(await notificationRows(), "Fake Payroll Read has expired");
    await user.click(
      within(row).getByRole("button", { name: "Mark read Fake Payroll Read has expired" }),
    );

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
  });
});

/**
 * An earlier change §6, added to 23gb/23gc's file and rewriting none of it (§8): the
 * three cases that hold the PAGE read still, after its four private lines
 * become `readWire(url, notificationPageSchema)`. The read stamp is a POST, not
 * a read, so it keeps its own `StampRefusedError` and the refusal cases above
 * are untouched.
 */
describe("NotificationList — the page read through the shared wire reader", () => {
  const ORIGIN = "http://localhost:3000";

  it("a 403 FORBIDDEN on the page read renders the page alert, not the code's sentence", async () => {
    // Decision B on the file that already renders `FAILURE_MESSAGES` sentences
    // for a refused STAMP, in a row-level alert. Kills a migration reusing that
    // dressing for the READ — "You are not allowed to do that." in the region
    // alert reads as "you may not see your notifications", where the generic
    // sentence tells the person to try again — and kills a `WireRefusedError`
    // escaping the error branch, which leaves the page on its loading state with
    // no alert and no list. The two rows above cannot decide it: a refused
    // payload and a 502 both arrive as plain errors through the migrated reader,
    // so only a typed body reaches the new class. The row-level alert is absent
    // here by construction (no stamp is clicked), so the region alert is the
    // only one this case can find.
    pageAnswer = () => jsonResponse({ code: "FORBIDDEN" }, 403);
    renderList();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(PAGE_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.FORBIDDEN);
    expect(screen.queryByRole("list", { name: "Your notifications" })).toBeNull();
    expect(screen.queryByText(EMPTY_LIST)).toBeNull();
  }, 10_000);

  it("reads /api/me/notifications with an empty query string on first paint", async () => {
    // The page read and the bell's read are the SAME path and are told apart by
    // the query alone (decision E: no `limit`, and `unread_only` only when the
    // control is on), so this is the one assertion that can catch them being
    // swapped. Kills a migration that gives the page read the bell's
    // `?unread_only=true&limit=1` — the list would then show one unread row and
    // call it everything — and kills one that sends `unread_only=false`, a value
    // the tool has no reading for. `toContain` would pass for all of those,
    // which is why the `search` is exact. Arithmetic: the
    // staged answer is the default empty 200 and no bell is rendered in
    // `renderList`, so reads to `/api/me/notifications` = 1.
    pageAnswer = () => jsonResponse({ notifications: [], unread_count: 0 }, 200);
    renderList();

    await screen.findByText(EMPTY_LIST);
    const url = new URL(
      only(
        callsMatching((candidate) => candidate.includes("/api/me/notifications")),
        "notifications read",
      ).url,
      ORIGIN,
    );
    expect(url.pathname).toBe("/api/me/notifications");
    expect(url.search).toBe("");
  });

  it("a bodyless 500 on the page read renders the same page alert", async () => {
    // §5's second risk on this page: `readWire` reads the body of every non-ok
    // response looking for a typed code, and `fetchNotifications` reads it for
    // none. Kills a refusal branch awaiting an unguarded `json()`, and kills one
    // classifying an unreadable body as a refusal carrying `undefined`, whose
    // `failureMessage` is the default sentence and says nothing about
    // notifications. The empty state must stay absent: "You have no
    // notifications." over an outage is what §5's fourth risk names, and it is
    // the reading a person acts on by closing the tab.
    pageAnswer = () => new Response(null, { status: 500 });
    renderList();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(PAGE_ALERT);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.default);
    expect(screen.queryByRole("list", { name: "Your notifications" })).toBeNull();
    expect(screen.queryByText(EMPTY_LIST)).toBeNull();
  }, 10_000);
});
