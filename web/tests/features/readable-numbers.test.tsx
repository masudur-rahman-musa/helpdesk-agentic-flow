import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { ApprovalQueue } from "@/features/approvals/components/approval-queue";
import { DecisionCard } from "@/features/chat/components/decision-card";
import { readDecision, type DecisionCard as Card } from "@/features/chat/components/types";
import { RequestList } from "@/features/requests/components/request-list";

/**
 * `readable-numbers.test.tsx`: every part that names a request shows its short
 * number (`REQ-0107`) and never the database id. jsdom, a stubbed `fetch`, no
 * backing service.
 *
 * Shapes this file fixes:
 * - the wire rows carry `request_number`, a string like `REQ-0107`
 *   (`web/src/features/access/schema.ts`, `web/src/features/approvals/schema.ts`).
 * - the chat card type carries `requestNumber: string | null`, null while
 *   nothing is filed.
 * - the id stays in code: the card keeps `data-request-id`, so a test reads
 *   text, not attributes, to look for a leaked id.
 */

const UUID_SHAPE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

const REQUEST_ONE = "66666666-6666-5666-8666-666666666666";
const REQUEST_TWO = "88888888-8888-5888-8888-888888888888";

function requestRow(id: string, number: string): Record<string, unknown> {
  return {
    request_id: id,
    request_number: number,
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
    decision_reasons: ["NOT_IN_TEMPLATE"],
    citations: [],
    approvals: [{ slot: "department_manager", user_id: null }],
    grant_id: null,
    expires_at: null,
  };
}

function approvalRow(id: string, number: string): Record<string, unknown> {
  return {
    request_id: id,
    request_number: number,
    requester_user_id: "22222222-2222-5222-8222-222222222222",
    target_user_id: "33333333-3333-5333-8333-333333333333",
    role_id: "77777777-7777-5777-8777-777777777777",
    justification: "Obviously fake justification.",
    requested_expiry_days: 30,
    risk_class: "elevated",
    decision: "approval",
    status: "pending",
    created_at: "2026-06-02T10:15:00Z",
    my_slot: "it_admin",
    approvals: [{ slot: "it_admin", user_id: "44444444-4444-5444-8444-444444444444" }],
    role_slug: "payroll-read",
    role_name: "Fake Payroll Read",
    system_slug: "payroll",
    system_name: "Fake Payroll",
    target_display_name: "Fake Person One",
    requester_display_name: "Fake Person Two",
    decision_reasons: ["PRIVILEGE_ADMIN"],
    citations: [],
  };
}

const fetchStub = vi.fn<typeof fetch>();
let body: unknown = {};

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  fetchStub.mockReset();
  fetchStub.mockImplementation(async () => jsonResponse(body));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** The list's own rows: its DIRECT listitem children, never a nested list's. */
function rowsOf(list: HTMLElement): HTMLElement[] {
  return within(list)
    .getAllByRole("listitem")
    .filter((item) => item.parentElement === list);
}

function at(elements: HTMLElement[], index: number): HTMLElement {
  const element = elements[index];
  if (element === undefined) {
    throw new Error(`expected an element at index ${index}, found ${elements.length}`);
  }
  return element;
}

function card(overrides: Partial<Card>): Card {
  return {
    requestId: REQUEST_ONE,
    requestNumber: "REQ-0107",
    status: "pending",
    decision: "approval",
    riskClass: "privileged",
    reasons: ["PRIVILEGE_ADMIN"],
    approvers: [{ slot: "department_manager", resolved: true }],
    effectiveExpiryDays: 90,
    expiresAt: null,
    ...overrides,
  };
}

describe("readable numbers on screen", () => {
  it("the request list shows REQ-0107 and no uuid", async () => {
    // Catches a list that keeps printing the database id (or prints nothing
    // for the row's name): the row's text must hold the number from the wire,
    // and no UUID-shaped string anywhere in the text a person reads.
    body = { requests: [requestRow(REQUEST_ONE, "REQ-0107")] };
    render(
      <QueryProvider>
        <RequestList />
      </QueryProvider>,
    );

    const region = await screen.findByRole("region", { name: "My requests" });
    const list = await within(region).findByRole("list", { name: "Requests" });
    const row = at(rowsOf(list), 0);

    expect(row).toHaveTextContent("REQ-0107");
    expect(row.textContent ?? "").not.toMatch(UUID_SHAPE);
    expect(region.textContent ?? "").not.toMatch(UUID_SHAPE);
  });

  it("the approval queue shows the number on each row", async () => {
    // Catches a queue that shows one number for every row (the first, or a
    // constant) or none: each row must carry its own, and no uuid text.
    body = {
      approvals: [approvalRow(REQUEST_ONE, "REQ-0001"), approvalRow(REQUEST_TWO, "REQ-0002")],
    };
    render(
      <QueryProvider>
        <ApprovalQueue />
      </QueryProvider>,
    );

    const region = await screen.findByRole("region", { name: "Approvals" });
    const list = await within(region).findByRole("list", { name: "Pending approvals" });
    const rows = rowsOf(list);

    expect(rows).toHaveLength(2);
    expect(at(rows, 0)).toHaveTextContent("REQ-0001");
    expect(at(rows, 0)).not.toHaveTextContent("REQ-0002");
    expect(at(rows, 1)).toHaveTextContent("REQ-0002");
    expect(at(rows, 1)).not.toHaveTextContent("REQ-0001");
    expect(region.textContent ?? "").not.toMatch(UUID_SHAPE);
  });

  it("a filed decision card shows its number", () => {
    // Catches a card that leaves the number out, and one that invents a number
    // for a request nothing filed: the filed card shows REQ-0107, the unfiled
    // one shows "Not filed yet" and no REQ- text at all. The id stays only in
    // the attribute, never in the text.
    const { unmount } = render(<DecisionCard card={card({})} />);
    const filed = screen.getByRole("region", { name: "Decision" });
    expect(filed).toHaveTextContent("REQ-0107");
    expect(filed.textContent ?? "").not.toMatch(UUID_SHAPE);
    expect(filed).toHaveAttribute("data-request-id", REQUEST_ONE);
    unmount();

    render(
      <DecisionCard
        card={card({ requestId: null, requestNumber: null, status: null, approvers: [] })}
      />,
    );
    const unfiled = screen.getByRole("region", { name: "Decision" });
    expect(unfiled).toHaveTextContent("Not filed yet");
    expect(unfiled.textContent ?? "").not.toMatch(/REQ-/);
  });

  it("the reply frame's request_number reaches the card as requestNumber", () => {
    // Catches a reader that drops the new key (the card would then never show
    // a number): a wire card with `request_number` gives `requestNumber`, and
    // one with null or no such key gives null, never the string "undefined".
    const wire = {
      request_id: REQUEST_ONE,
      request_number: "REQ-0107",
      status: "pending",
      decision: "approval",
      risk_class: "privileged",
      reasons: [],
      approvers: [],
      effective_expiry_days: 90,
      expires_at: null,
    };

    expect(readDecision(wire)?.requestNumber).toBe("REQ-0107");
    expect(readDecision({ ...wire, request_number: null })?.requestNumber).toBeNull();
    const without: Record<string, unknown> = { ...wire };
    delete without.request_number;
    expect(readDecision(without)?.requestNumber).toBeNull();
  });
});
