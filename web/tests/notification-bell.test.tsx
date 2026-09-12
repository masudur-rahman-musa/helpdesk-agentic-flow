import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationBell } from "@/components/notifications/notification-bell";
import { QueryProvider } from "@/components/query-provider";

/**
 * An earlier change §6, `notification-bell.test.tsx`: the unread count in the shell's
 * nav, against a stubbed `fetch` and a frozen clock. jsdom, no agent-api, no
 * backing service. (No basename collision in the tree, a project rule.)
 *
 * The unit under test is `NotificationBell` from
 * `web/components/notifications/notification-bell.tsx`, taking NO props and
 * holding its own query (decision B), so `components/nav.tsx` stays the plain
 * component `tests/shell.test.tsx` renders with no router.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 23gc's Deviations):
 * - the bell is ONE `role="link"` whose accessible name is `Notifications, {n}
 *   unread` at every count, zero included — §1 fixes the name unconditionally
 *   and makes only the count ELEMENT disappear at zero. The count is therefore
 *   an `aria-label` (or equivalent) on the link and the rendered TEXT carries
 *   the number only when there is one, which is how "no count element at all"
 *   is observable: at zero the link's text holds no digit.
 * - a FAILED read leaves the count unknown, so no case pins the name then; the
 *   link is located by a name starting `Notifications` and asserted to carry no
 *   digit and no alert.
 * - the clock is frozen with `vi.useFakeTimers({ shouldAdvanceTime: true })`,
 *   `access-page.test.tsx`' shape: `shouldAdvanceTime` is load-bearing rather
 *   than decorative, because TanStack Query's retry delay and Testing Library's
 *   `waitFor` both need time to pass and a fully frozen clock hangs every async
 *   case here.
 *
 * The wire shape is 23gb's as BUILT and every fixture parses under
 * `web/lib/notifications-schema.ts`: `{ notifications: [...], unread_count: N }`,
 * with a failed proxy answering `502 {"code": "UPSTREAM_UNAVAILABLE"}`.
 *
 * §6 names no case for the "on window focus" half of decision D, and none is
 * written: TanStack Query v5 refetches from `visibilitychange` on `window` and
 * only once the query is stale, so a case for it would pin the library's own
 * default through its own mechanism rather than anything this component
 * decides. That gap is reported to 23gc rather than filled here.
 */

const LOGIN_URL = "http://localhost:3000/login";

/** One unread row with a tally of SEVEN: the bell renders the tally, never the
 * length of what `limit=1` handed it (§5's first risk). */
const UNREAD_ROW: Record<string, unknown> = {
  notification_id: "11111111-1111-5111-8111-111111111111",
  kind: "grant_expired",
  title: "Fake Payroll Read has expired",
  body: "Your access to Fake Payroll ended on the first.",
  subject_type: "grant",
  subject_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
  created_at: "2026-06-01T09:00:00Z",
  read_at: null,
};

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

let bellAnswer: () => Response = () => jsonResponse({ notifications: [], unread_count: 0 }, 200);

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The 200 a browser holds after FOLLOWING the proxy's redirect to `/login`
 *: neither `redirected` nor `url` is a constructor option. */
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

function renderBell(): void {
  render(
    <QueryProvider>
      <NotificationBell />
    </QueryProvider>,
  );
}

function bellCalls(): string[] {
  return fetchStub.mock.calls
    .map(([input]) => urlOf(input))
    .filter((url) => url.includes("/api/me/notifications"));
}

function onlyCall(): string {
  const calls = bellCalls();
  if (calls.length !== 1) {
    throw new Error(`expected exactly one bell read, found ${calls.length}`);
  }
  const first = calls[0];
  if (first === undefined) {
    throw new Error("expected exactly one bell read, found none");
  }
  return first;
}

async function bellLink(): Promise<HTMLElement> {
  return await screen.findByRole("link", { name: /^Notifications/ });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-06-05T12:00:00Z"));
  bellAnswer = () => jsonResponse({ notifications: [], unread_count: 0 }, 200);
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes("/api/me/notifications")) {
      return bellAnswer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", { ...window.location, href: "http://localhost:3000/chat", assign });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("NotificationBell — the count it reads", () => {
  it("reads unread_only=true&limit=1 and renders the tally, not the rows it was handed", async () => {
    // §5's first risk, which is the way this component goes quiet without
    // looking broken: a bell rendering `notifications.length` shows 1 while
    // seven rows wait, and at any count above the page size it would understate
    // every time. The fixture answers ONE row with a tally of SEVEN, so only a
    // bell reading `unread_count` (23ga decision B) can render 7. The url is
    // the other half of decision C: a bell without `unread_only=true` counts
    // nothing meaningful, and one without `limit=1` drags fifty rows it renders
    // none of into the header of every authenticated page. Arithmetic (standing
    // rule 43): bell reads = 1 on mount, `unread_count` = 7.
    bellAnswer = () => jsonResponse({ notifications: [UNREAD_ROW], unread_count: 7 }, 200);
    renderBell();

    expect(
      await screen.findByRole("link", { name: "Notifications, 7 unread" }),
    ).toBeInTheDocument();
    const url = onlyCall();
    expect(url).toContain("/api/me/notifications");
    expect(url).toContain("unread_only=true");
    expect(url).toContain("limit=1");
  });

  it("names the link Notifications, {n} unread and points it at /notifications", async () => {
    // §1's bell criterion as the element itself. Kills a bell whose count is
    // rendered as bare text beside an icon — a screen reader then hears
    // "Notifications" and a stray "7" with nothing joining them — and kills one
    // that renders the number but links nowhere, or links to `/chat`, which
    // makes the count a dead end: the whole point of the bell is that it is the
    // route into the page. The text assertion is what pins the number as
    // VISIBLE too, so an aria-label-only bell that shows no badge is red here.
    bellAnswer = () => jsonResponse({ notifications: [UNREAD_ROW], unread_count: 7 }, 200);
    renderBell();

    const link = await bellLink();
    expect(link).toHaveAccessibleName("Notifications, 7 unread");
    expect(link).toHaveAttribute("href", "/notifications");
    expect(link.textContent ?? "").toContain("7");
  });

  it("renders the link with no count element at unread_count 0", async () => {
    // §1's zero case. Kills a bell that paints a badge reading `0` on every
    // screen of an app where nothing is waiting — the one state in which the
    // header should say nothing at all — and kills one that hides the whole
    // LINK at zero, which would take the route away exactly when someone wants
    // to check their history. The digit assertion is what distinguishes the two
    // from the accessible name, which §1 keeps as `Notifications, {n} unread`
    // at every count.
    bellAnswer = () => jsonResponse({ notifications: [], unread_count: 0 }, 200);
    renderBell();

    const link = await screen.findByRole("link", { name: "Notifications, 0 unread" });
    expect(link).toHaveAttribute("href", "/notifications");
    expect(/\d/.test(link.textContent ?? "")).toBe(false);
  });
});

describe("NotificationBell — a read that fails", () => {
  const FAILURES = [
    {
      name: "a payload the parser refuses",
      answer: (): Response => jsonResponse({ notifications: [] }, 200),
    },
    {
      name: "the proxy's own 502",
      answer: (): Response => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ] as const;

  it.each(FAILURES)(
    "$name leaves the link in place with no count and no alert",
    async (row) => {
      // §5's third risk, and the one failure mode that follows a person from
      // screen to screen: the bell is in the header of the whole authenticated
      // app, so a page-shaped error region here puts "could not be read" above
      // every route, including the ones that are working. Kills a bell reusing
      // the page's alert rendering, and kills one that renders its error as a
      // count (`NaN`, `undefined` or the literal "0" claiming nothing is
      // waiting when the truth is unknown). The link surviving is the third
      // kill: a bell that unmounts on failure loses the route into the page.
      // The first row is the parser half — `unread_count` missing is exactly
      // what a proxy change would send — and the second the transport half.
      bellAnswer = row.answer;
      renderBell();

      const link = await screen.findByRole("link", { name: /^Notifications/ }, { timeout: 5_000 });
      expect(link).toHaveAttribute("href", "/notifications");
      expect(/\d/.test(link.textContent ?? "")).toBe(false);
      expect(screen.queryByRole("alert")).toBeNull();
    },
    10_000,
  );
});

describe("NotificationBell — freshness", () => {
  it("does not poll: no second read after two minutes of clock", async () => {
    // Decision D as behaviour rather than as a prop, which §6 asks for on
    // purpose: asserting a `refetchInterval` of `undefined` would pass for a
    // bell that polls through its own `setInterval`. Kills any polling bell —
    // the header is on every authenticated page, so an interval multiplies MCP
    // round trips by tabs and by minutes for a table four scheduler jobs write
    // to a few times a day, and the cost is invisible in a test that only looks
    // at the rendering. Arithmetic: bell reads = 1 on mount
    // and still 1 after 120_000ms of fake clock; a 30s interval would have made
    // it 5, a 60s one 3.
    bellAnswer = () => jsonResponse({ notifications: [UNREAD_ROW], unread_count: 7 }, 200);
    renderBell();

    await screen.findByRole("link", { name: "Notifications, 7 unread" });
    expect(bellCalls()).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(120_000);

    expect(bellCalls()).toHaveLength(1);
  });
});

describe("NotificationBell — an expired session", () => {
  it("follows the proxy's redirect to /login", async () => {
    // §1's last unhappy path at the bell read, the third of the plan's three
    // call sites and the one that fires on EVERY authenticated page, so it is
    // the likeliest place a stale session is discovered at all. It is
    // `response.redirected` and never a status (22b decision E, standing rule
    // 59): the browser has already followed the redirect, so what arrives is a
    // 200 carrying the login page. Kills a bell whose fetcher checks
    // `response.ok` or `response.status === 401` — both see a success, the
    // parser refuses the HTML, and the person keeps browsing an app whose every
    // read is about to fail with no explanation. This component's own read is
    // the only fetch in the tree here, so nothing else can make the assignment
    // happen.
    bellAnswer = redirectedResponse;
    renderBell();

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
  });
});

/**
 * An earlier change §6, added to 23gc's file and rewriting none of it (§8): the three
 * cases that hold `fetchUnreadCount` still, after its four private lines become
 * `readWire("/api/me/notifications?unread_only=true&limit=1",
 * notificationPageSchema)` with `null` mapped to `0`.
 *
 * §6 asks each read's case to render "that page's existing alert sentence".
 * This component has NONE and may not grow one: §1 of 23gc makes a failed read
 * silent, because an error region in the shell's header follows a person onto
 * every route including the working ones. The failure state pinned here is
 * therefore that silence — the link still present, its accessible name
 * `Notifications` with no count in it, and no alert anywhere — which is the
 * state this bell reaches today. Reported to 24ia as a deviation rather than
 * answered by adding a sentence the plan's §1 forbids.
 */
describe("NotificationBell — through the shared wire reader", () => {
  const ORIGIN = "http://localhost:3000";

  it("a 403 FORBIDDEN leaves the count unknown, never 0, and shows no alert", async () => {
    // Decision B where this component has no sentence to keep, so what has to
    // hold is the silence AND the unknown count. The accessible name is the
    // whole discriminator: a bell that maps the refusal (or `readWire`'s `null`)
    // to `0` renders `Notifications, 0 unread`, which claims nothing is waiting
    // when the truth is that nobody could read it — and `0` is exactly what the
    // migration's `?? 0` invites at the wrong place, since §1 maps only the
    // REDIRECT to zero. Kills that, kills a bell dressing the typed code into an
    // alert in the shell's header (the one place an error region follows a
    // person from screen to screen), and kills a `WireRefusedError` unmounting
    // the link, which takes away the route into the page. The typed body is what
    // makes the new class reachable: the 502 row above is a plain error through
    // the migrated reader too.
    bellAnswer = () => jsonResponse({ code: "FORBIDDEN" }, 403);
    renderBell();

    const link = await screen.findByRole("link", { name: "Notifications" }, { timeout: 5_000 });
    expect(link).toHaveAttribute("href", "/notifications");
    expect(/\d/.test(link.textContent ?? "")).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("link", { name: "Notifications, 0 unread" })).toBeNull();
  }, 10_000);

  it("reads /api/me/notifications with the exact query string ?unread_only=true&limit=1", async () => {
    // The bell's query string is the only thing telling its read apart from the
    // page's, on the same path (decision C against decision E). The case above
    // asserts the two keys with `toContain`, which stays green for
    // `?unread_only=true&limit=50` or for a third parameter added while the call
    // moves; this pins the `search` byte for byte, so a migration that drops
    // `limit=1` (fifty rows dragged into the header of every authenticated page,
    // for a number) or drops `unread_only=true` (a tally of everything, read as
    // unread) is red. Arithmetic: the staged answer is the
    // default `unread_count: 0` 200, so bell reads = 1 on mount, and the named
    // link is what says the read settled before the count is taken.
    bellAnswer = () => jsonResponse({ notifications: [], unread_count: 0 }, 200);
    renderBell();

    await screen.findByRole("link", { name: "Notifications, 0 unread" });
    const url = new URL(onlyCall(), ORIGIN);
    expect(url.pathname).toBe("/api/me/notifications");
    expect(url.search).toBe("?unread_only=true&limit=1");
  });

  it("a bodyless 500 leaves the count unknown, never 0, and shows no alert", async () => {
    // §5's second risk on the read that fires on every authenticated page:
    // `readWire` reads the body of every non-ok response looking for a typed
    // code, and `fetchUnreadCount` reads it for none. Kills a refusal branch
    // awaiting an unguarded `json()` — the rejection would surface as an
    // unhandled one rather than as this query's error — and kills a bell that
    // answers an unreadable failure with `0`, which is the difference between
    // "nothing is waiting" and "nobody knows" on every screen of the app.
    bellAnswer = () => new Response(null, { status: 500 });
    renderBell();

    const link = await screen.findByRole("link", { name: "Notifications" }, { timeout: 5_000 });
    expect(link).toHaveAttribute("href", "/notifications");
    expect(/\d/.test(link.textContent ?? "")).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("link", { name: "Notifications, 0 unread" })).toBeNull();
  }, 10_000);
});
