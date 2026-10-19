import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RequestsPage from "@/app/(app)/requests/page";
import { QueryProvider } from "@/components/layout/query-provider";
import { TicketList } from "@/features/tickets/components/ticket-list";

/**
 * `ticket-list.test.tsx`: the small My tickets list against a stubbed `fetch`.
 * jsdom, no agent-api, no backing service.
 *
 * Shapes this file fixes (the plan left them open):
 * - the unit is `TicketList` from `features/tickets/components/ticket-list.tsx`,
 *   taking no props and reading `/api/me/tickets` itself, like `RequestList`.
 * - accessible names: region `My tickets`, row list `Tickets`.
 * - status words are `Open` and `Solved`; a kind is shown in plain words, so
 *   `password_reset` reads as "Password reset" (matched ignoring case, with no
 *   underscore).
 * - the empty sentence and the alert sentence are the plan's, byte for byte.
 */

const EMPTY_TICKETS = "You have not opened any tickets.";
const READ_ALERT = "Your tickets could not be read. Try again in a moment.";
const TICKET_ID = "88888888-8888-5888-8888-888888888888";
const OTHER_TICKET_ID = "99999999-9999-5999-8999-999999999999";

const OPEN_ROW = {
  ticket_id: TICKET_ID,
  ticket_number: "TCK-0042",
  kind: "password_reset",
  title: "Fake locked out title",
  status: "open",
  created_at: "2026-06-02T10:15:00Z",
};
const SOLVED_ROW = {
  ticket_id: OTHER_TICKET_ID,
  ticket_number: "TCK-0007",
  kind: "general",
  title: "Fake printer title",
  status: "solved",
  created_at: "2026-05-01T08:00:00Z",
};

const fetchStub = vi.fn<typeof fetch>();
let ticketsAnswer: () => Response = () => jsonResponse({ tickets: [] }, 200);

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function urlOf(input: Parameters<typeof fetch>[0]): string {
  return typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
}

beforeEach(() => {
  ticketsAnswer = () => jsonResponse({ tickets: [] }, 200);
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes("/api/me/tickets")) {
      return ticketsAnswer();
    }
    if (url.includes("/api/me/requests")) {
      return jsonResponse({ requests: [] }, 200);
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderList(): void {
  render(
    <QueryProvider>
      <TicketList />
    </QueryProvider>,
  );
}

describe("TicketList", () => {
  it("shows each ticket by number, title, kind and status", async () => {
    // Kill: catches a row that shows the database id instead of the `TCK-`
    // number, one that prints the wire words (`password_reset`, `solved` in
    // lower case), one that renders only the first ticket, and one that drops
    // the status (the whole point of the list). The id text is checked in the
    // whole region so it cannot hide in an attribute-looking sentence.
    ticketsAnswer = () => jsonResponse({ tickets: [OPEN_ROW, SOLVED_ROW] }, 200);
    renderList();

    const region = await screen.findByRole("region", { name: "My tickets" });
    const list = await within(region).findByRole("list", { name: "Tickets" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);

    const [first, second] = rows;
    expect(first).toHaveTextContent("TCK-0042");
    expect(first).toHaveTextContent("Fake locked out title");
    expect(first).toHaveTextContent(/password reset/i);
    expect(first).toHaveTextContent("Open");
    expect(second).toHaveTextContent("TCK-0007");
    expect(second).toHaveTextContent("Fake printer title");
    expect(second).toHaveTextContent("Solved");

    const text = region.textContent ?? "";
    expect(text).not.toContain(TICKET_ID);
    expect(text).not.toContain(OTHER_TICKET_ID);
    expect(text).not.toContain("password_reset");
    expect(screen.queryByText(EMPTY_TICKETS)).toBeNull();
  });

  it("shows the empty sentence when there are no tickets", async () => {
    // Kill: catches an empty list drawn under the heading with no words, which
    // reads the same as a broken read. The absent list is the half a bare
    // sentence beside an empty `ul` would not satisfy.
    ticketsAnswer = () => jsonResponse({ tickets: [] }, 200);
    renderList();

    expect(await screen.findByText(EMPTY_TICKETS)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Tickets" })).toBeNull();
  });

  it("shows an alert, not the empty sentence, when the read fails", async () => {
    // Kill: catches a page that turns a failed read into an empty list, saying
    // "you have not opened any tickets" over tickets the person may have. The
    // 502 envelope is valid JSON, so a reader that skips the status check
    // would also pass it on as data.
    ticketsAnswer = () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502);
    renderList();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(READ_ALERT);
    expect(screen.queryByText(EMPTY_TICKETS)).toBeNull();
    expect(screen.queryByRole("list", { name: "Tickets" })).toBeNull();
  }, 10_000);
});

describe("the requests page", () => {
  it("the requests page shows My tickets below My requests", async () => {
    // Kill: catches a tickets list left out of the page (the new tool would
    // have no door), one that gets its own tab instead of joining this page,
    // and one drawn above My requests. Order is read from the document, not
    // from the order the reads finish.
    render(
      <QueryProvider>
        <RequestsPage />
      </QueryProvider>,
    );

    const requests = await screen.findByRole("heading", { name: "My requests" });
    const tickets = await screen.findByRole("heading", { name: "My tickets" });
    expect(
      requests.compareDocumentPosition(tickets) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
