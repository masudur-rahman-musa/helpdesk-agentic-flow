import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { RequestList } from "@/features/requests/components/request-list";
import { notificationKindLabel } from "@/features/notifications/labels";
import { statusLabel } from "@/lib/decision-labels";

/**
 * `cancelled-status.test.tsx`: a request the person took back shows as
 * "Cancelled", never as "Rejected", and its notice reads "Request cancelled".
 * jsdom, a stubbed `fetch`, no agent-api and no backing service.
 *
 * The row is the same wire shape `requests-page.test.tsx` uses, with only the
 * status changed.
 *
 * Kill note on the tone: `statusTone` already answers `neutral` for any status
 * it does not know, so the tone assertion alone cannot fail before the map
 * gains `cancelled`. The label assertions are what fail first; the tone is
 * pinned so a later change to `danger` (the colour of "Rejected") is caught.
 */

const CANCELLED_ROW: Record<string, unknown> = {
  request_id: "66666666-6666-5666-8666-666666666666",
  request_number: "REQ-0107",
  target_user_id: "11111111-1111-5111-8111-111111111111",
  role_id: "77777777-7777-5777-8777-777777777777",
  justification: "Obviously fake justification.",
  requested_expiry_days: 30,
  risk_class: "elevated",
  decision: "approval",
  status: "cancelled",
  created_at: "2026-06-02T10:15:00Z",
  role_slug: "viewer",
  role_name: "Fake CRM viewer",
  system_slug: "crm",
  system_name: "Fake CRM",
  target_display_name: "Fake Person One",
  decision_reasons: ["NOT_IN_TEMPLATE"],
  citations: [],
  approvals: [{ slot: "it_admin", user_id: null }],
  grant_id: null,
  expires_at: null,
};

const fetchStub = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : "";
    if (url.includes("/api/me/requests")) {
      return new Response(JSON.stringify({ requests: [CANCELLED_ROW] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a cancelled request", () => {
  it('shows a cancelled request as "Cancelled" in the neutral tone, never "Rejected"', async () => {
    // Catches a page that prints the raw wire value ("cancelled"), one that
    // files a cancel under "Rejected" (the danger colour), and a status map
    // that leaves `cancelled` out.
    render(
      <QueryProvider>
        <RequestList />
      </QueryProvider>,
    );

    const region = await screen.findByRole("region", { name: "My requests" });
    const list = await within(region).findByRole("list", { name: "Requests" });
    const badge = await within(list).findByText("Cancelled");

    expect(badge.getAttribute("data-tone")).toBe("neutral");
    expect(within(list).queryByText("Rejected")).toBeNull();
    expect(within(list).queryByText("cancelled")).toBeNull();
  });

  it("has its label in the status map", () => {
    // Catches the label living only in a page-local branch: the shared lookup
    // every other page reads must also say "Cancelled".
    expect(statusLabel("cancelled")).toBe("Cancelled");
  });
});

describe("a request_cancelled notice", () => {
  it('labels a request_cancelled notice "Request cancelled"', () => {
    // Catches a kind left out of the label map, which shows the raw code at
    // the approver, and one filed under the rejected wording.
    expect(notificationKindLabel("request_cancelled")).toBe("Request cancelled");
  });
});
