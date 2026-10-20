import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { MyRequestsPanel } from "@/features/chat/components/my-requests-panel";
import { statusLabel } from "@/lib/decision-labels";

/**
 * `my-requests-panel.test.tsx`: the panel beside the chat that lists the
 * person's own access requests and tickets. jsdom, a stubbed
 * `fetch`, no agent-api.
 *
 * Shapes this file fixes:
 * - the unit is `MyRequestsPanel` from
 *   `web/src/features/chat/components/my-requests-panel.tsx`, taking no props
 *   and reading `/api/me/requests` and `/api/me/tickets` itself, like
 *   `RequestList` and `TicketList`.
 * - the status shows through `statusLabel`, the words the requests page uses.
 * - the empty lines are "No access requests yet." and "No tickets yet.".
 * - a failed read is a `role="alert"` element.
 */

const EMPTY_REQUESTS = "No access requests yet.";
const EMPTY_TICKETS = "No tickets yet.";

const REQUEST_ROW: Record<string, unknown> = {
  request_id: "66666666-6666-5666-8666-666666666666",
  request_number: "REQ-0001",
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
  decision_reasons: [],
  citations: [],
  approvals: [],
  grant_id: null,
  expires_at: null,
};

const TICKET_ROW = {
  ticket_id: "88888888-8888-5888-8888-888888888888",
  ticket_number: "TCK-0042",
  kind: "password_reset",
  title: "Fake locked out title",
  status: "open",
  created_at: "2026-06-02T10:15:00Z",
};

const fetchStub = vi.fn<typeof fetch>();
let requestsAnswer: () => Response = () => json({ requests: [] }, 200);
let ticketsAnswer: () => Response = () => json({ tickets: [] }, 200);

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function urlOf(input: Parameters<typeof fetch>[0]): string {
  return typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
}

beforeEach(() => {
  requestsAnswer = () => json({ requests: [] }, 200);
  ticketsAnswer = () => json({ tickets: [] }, 200);
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes("/api/me/requests")) {
      return requestsAnswer();
    }
    if (url.includes("/api/me/tickets")) {
      return ticketsAnswer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderPanel(): void {
  render(
    <QueryProvider>
      <MyRequestsPanel />
    </QueryProvider>,
  );
}

describe("MyRequestsPanel", () => {
  it("shows own requests with status and own tickets", async () => {
    // Catches a panel that lists only one of the two kinds, or drops a field
    // the person needs to find the item again: the request number, the role
    // name and its status, the ticket number and its title. The status goes
    // through the label helper, so a raw engine word like "pending" fails.
    requestsAnswer = () => json({ requests: [REQUEST_ROW] }, 200);
    ticketsAnswer = () => json({ tickets: [TICKET_ROW] }, 200);
    const { container } = render(
      <QueryProvider>
        <MyRequestsPanel />
      </QueryProvider>,
    );

    await screen.findByText("REQ-0001");
    const panel = within(container);
    expect(panel.getByText("Fake Payroll Read")).toBeInTheDocument();
    expect(panel.getByText(statusLabel("pending"))).toBeInTheDocument();
    expect(await panel.findByText("TCK-0042")).toBeInTheDocument();
    expect(panel.getByText("Fake locked out title")).toBeInTheDocument();
    expect(panel.queryByText(EMPTY_REQUESTS)).toBeNull();
    expect(panel.queryByText(EMPTY_TICKETS)).toBeNull();
  });

  it("shows an empty line for each list when there is nothing", async () => {
    // Catches a panel that shows a blank box (or one empty line for both
    // lists) when there is nothing: each list owns its own sentence.
    renderPanel();

    expect(await screen.findByText(EMPTY_REQUESTS)).toBeInTheDocument();
    expect(await screen.findByText(EMPTY_TICKETS)).toBeInTheDocument();
  });

  it("shows an alert when a list fails", async () => {
    // Catches a failed read shown as "No access requests yet.", a sentence a
    // person might act on, and a panel where one failing list blanks the other.
    // The tickets answer is fine, so only the requests list may complain.
    requestsAnswer = () => json({ code: "UPSTREAM_UNAVAILABLE" }, 500);
    ticketsAnswer = () => json({ tickets: [TICKET_ROW] }, 200);
    renderPanel();

    expect(await screen.findByRole("alert", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(await screen.findByText("Fake locked out title")).toBeInTheDocument();
    expect(screen.queryByText(EMPTY_REQUESTS)).toBeNull();
  });

  it("never calls the approvals route", async () => {
    // Catches a panel that reads the approvals queue: approvals waiting on the
    // person are what OTHERS asked of them, and the panel must show only their
    // own requests and tickets. Only the two own-list routes may be fetched.
    renderPanel();
    await screen.findByText(EMPTY_REQUESTS);
    await screen.findByText(EMPTY_TICKETS);

    await waitFor(() => expect(fetchStub).toHaveBeenCalled());
    const urls = new Set(
      fetchStub.mock.calls.map(([input]) => new URL(urlOf(input), "http://x").pathname),
    );
    expect(urls).toEqual(new Set(["/api/me/requests", "/api/me/tickets"]));
  });
});
