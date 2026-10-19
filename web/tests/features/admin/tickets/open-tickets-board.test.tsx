import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { OpenTicketsBoard } from "@/features/admin/tickets/components/open-tickets-board";
import { FAILURE_MESSAGES } from "@/lib/failure-messages";

/**
 * `open-tickets-board.test.tsx`: the read-only `/admin/tickets` board against a
 * stubbed `fetch`. jsdom, no agent-api, no backing service.
 *
 * Shapes this file fixes (the plan left them open):
 * - the unit is `OpenTicketsBoard` from
 *   `features/admin/tickets/components/open-tickets-board.tsx`, taking no props
 *   and reading `/api/admin/tickets` itself, like `RulesBoard`.
 * - accessible names: region `Open tickets`, table `Open tickets`; the rows are
 *   the table's rows holding cells.
 * - a kind is shown in plain words (`password_reset` reads "Password reset",
 *   through `features/tickets/labels.ts`); the date is the ISO date of
 *   `created_at` (`2026-06-02`), through `lib/date-format`.
 * - the empty sentence and the alert sentence are the plan's, byte for byte;
 *   a typed refusal shows the shared `FAILURE_MESSAGES` sentence.
 */

const EMPTY_TICKETS = "No tickets are open.";
const READ_ALERT = "Open tickets could not be read. Try again in a moment.";
const TICKET_ID = "88888888-8888-5888-8888-888888888888";
const OTHER_TICKET_ID = "99999999-9999-5999-8999-999999999999";

const FIRST_ROW = {
  ticket_id: TICKET_ID,
  ticket_number: "TCK-0042",
  kind: "password_reset",
  title: "Fake locked out title",
  opened_by: "Fake Person One",
  created_at: "2026-06-02T10:15:00Z",
};
const SECOND_ROW = {
  ticket_id: OTHER_TICKET_ID,
  ticket_number: "TCK-0007",
  kind: "general",
  title: "Fake printer title",
  opened_by: "Fake Person Two",
  created_at: "2026-06-05T08:00:00Z",
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
    if (url.includes("/api/admin/tickets")) {
      return ticketsAnswer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderBoard(): void {
  render(
    <QueryProvider>
      <OpenTicketsBoard />
    </QueryProvider>,
  );
}

function missingTable(): HTMLElement | null {
  return screen.queryByRole("table", { name: "Open tickets" });
}

describe("OpenTicketsBoard", () => {
  it("shows each ticket by number, title, kind, opener and date", async () => {
    // Kill: catches a row that shows the database id instead of the `TCK-`
    // number, one that prints the wire word `password_reset`, one that renders
    // only the first ticket, one that drops the opener or the date, and one
    // that hides the id only in a cell while printing it elsewhere (the whole
    // region's text is checked). Rows keep the server's order, which is not
    // the order of number or title here, so a client-side re-sort is red too.
    ticketsAnswer = () => jsonResponse({ tickets: [FIRST_ROW, SECOND_ROW] }, 200);
    renderBoard();

    const region = await screen.findByRole("region", { name: "Open tickets" });
    const table = await within(region).findByRole("table", { name: "Open tickets" });
    const rows = within(table)
      .getAllByRole("row")
      .filter((row) => within(row).queryAllByRole("cell").length > 0);
    expect(rows).toHaveLength(2);

    const [first, second] = rows;
    expect(first).toHaveTextContent("TCK-0042");
    expect(first).toHaveTextContent("Fake locked out title");
    expect(first).toHaveTextContent("Password reset");
    expect(first).toHaveTextContent("Fake Person One");
    expect(first).toHaveTextContent("2026-06-02");
    expect(second).toHaveTextContent("TCK-0007");
    expect(second).toHaveTextContent("Fake printer title");
    expect(second).toHaveTextContent("General");
    expect(second).toHaveTextContent("Fake Person Two");
    expect(second).toHaveTextContent("2026-06-05");

    const text = region.textContent ?? "";
    expect(text).not.toContain(TICKET_ID);
    expect(text).not.toContain(OTHER_TICKET_ID);
    expect(text).not.toContain("password_reset");
    expect(screen.queryByText(EMPTY_TICKETS)).toBeNull();
  });

  it("makes one read of /api/admin/tickets with no body", async () => {
    // Kill: catches a board that reads a different path (the employee's own
    // list `/api/me/tickets`, which would show an admin their own tickets as
    // the company's), one that reads twice, and one that sends a body on a
    // read.
    ticketsAnswer = () => jsonResponse({ tickets: [FIRST_ROW] }, 200);
    renderBoard();

    await screen.findByRole("table", { name: "Open tickets" });

    const calls = fetchStub.mock.calls.filter(([input]) =>
      urlOf(input).includes("/api/admin/tickets"),
    );
    expect(calls).toHaveLength(1);
    const call = calls[0];
    if (call === undefined) {
      throw new Error("expected one read of the board");
    }
    expect(new URL(urlOf(call[0]), "http://localhost:3000").pathname).toBe("/api/admin/tickets");
    expect(call[1]?.body).toBeUndefined();
  });

  it("shows the empty sentence when no ticket is open", async () => {
    // Kill: catches an empty table drawn under the heading with no words,
    // which reads the same as a broken read. The absent table is the half a
    // bare sentence beside an empty table would not satisfy.
    ticketsAnswer = () => jsonResponse({ tickets: [] }, 200);
    renderBoard();

    expect(await screen.findByText(EMPTY_TICKETS)).toBeInTheDocument();
    expect(missingTable()).toBeNull();
  });

  it("shows an alert, not the empty sentence, when the read fails", async () => {
    // Kill: catches a page that turns a failed read into an empty list, saying
    // "no tickets are open" over tickets that may exist. The 502 envelope is
    // valid JSON, so a reader that skips the status check would also pass it
    // on as data. `UPSTREAM_UNAVAILABLE` is not a typed refusal, so the
    // page's own sentence is the one shown.
    ticketsAnswer = () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502);
    renderBoard();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(READ_ALERT);
    expect(screen.queryByText(EMPTY_TICKETS)).toBeNull();
    expect(missingTable()).toBeNull();
  }, 10_000);

  it("shows the refusal message when the read is forbidden", async () => {
    // Kill: catches a board that answers every failure with the "try again in
    // a moment" sentence (a person who is not an admin would be told to retry
    // something no retry fixes), and one that prints the code `FORBIDDEN`.
    // The sentence is READ from `FAILURE_MESSAGES`, so a board inventing its
    // own wording is red without widening the pinned map.
    ticketsAnswer = () => jsonResponse({ code: "FORBIDDEN" }, 403);
    renderBoard();

    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });
    expect(alert).toHaveTextContent(FAILURE_MESSAGES.FORBIDDEN);
    expect(screen.queryByText(READ_ALERT)).toBeNull();
    expect(screen.queryByText(EMPTY_TICKETS)).toBeNull();
    expect(missingTable()).toBeNull();
  }, 10_000);
});
