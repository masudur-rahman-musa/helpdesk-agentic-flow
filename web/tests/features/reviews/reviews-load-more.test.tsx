import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { ReviewQueue } from "@/features/reviews/components/review-queue";

/**
 * A review bigger than one page of items. The first read answers a cursor,
 * and "Load more items" must read the next page from it and show both pages,
 * until the server says there is no more.
 */

const REVIEW_ID = "11111111-1111-5111-8111-111111111111";
const FIRST_ITEM = "33333333-3333-5333-8333-333333333333";
const SECOND_ITEM = "44444444-4444-5444-8444-444444444444";

const ROW: Record<string, unknown> = {
  review_id: REVIEW_ID,
  period: "2026-Q2",
  status: "open",
  due_at: "2026-07-01T00:00:00Z",
  completed_at: null,
  department_id: "88888888-8888-5888-8888-888888888888",
  department_name: "Fake Finance",
  system_id: null,
  system_name: null,
  reviewer_user_id: "77777777-7777-5777-8777-777777777777",
  reviewer_display_name: "Fake Person One",
  item_count: 2,
  decided_count: 0,
};

function item(itemId: string, person: string): Record<string, unknown> {
  return {
    item_id: itemId,
    grant_id: itemId.replace(/^3|^4/, "a"),
    flag: "ok",
    decision: null,
    decided_at: null,
    target_display_name: person,
    role_name: "Fake Payroll Read",
    system_name: "Fake Payroll",
    privilege: "read",
    granted_at: "2025-11-03T09:00:00Z",
    expires_at: null,
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  return input instanceof URL ? input.toString() : input.url;
}

const fetchStub = vi.fn<typeof fetch>();

function itemUrls(): string[] {
  return fetchStub.mock.calls
    .map(([input]) => urlOf(input))
    .filter((url) => url.includes("/items"));
}

beforeEach(() => {
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes("/api/me/reviews")) {
      return Promise.resolve(jsonResponse({ reviews: [ROW] }));
    }
    if (url.includes(`/api/reviews/${REVIEW_ID}/items`)) {
      const after = new URL(url, "http://localhost").searchParams.get("after");
      return Promise.resolve(
        after === null
          ? jsonResponse({
              review_id: REVIEW_ID,
              items: [item(FIRST_ITEM, "Fake Person Three")],
              next_after_item_id: FIRST_ITEM,
            })
          : jsonResponse({
              review_id: REVIEW_ID,
              items: [item(SECOND_ITEM, "Fake Person Four")],
              next_after_item_id: null,
            }),
      );
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ReviewItems — more than one page", () => {
  it("reads the next page from the cursor and keeps the first page on screen", async () => {
    const user = userEvent.setup();
    render(
      <QueryProvider>
        <ReviewQueue />
      </QueryProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "Show items for 2026-Q2" }));
    const list = await screen.findByRole("list", { name: "Items in review" });
    expect(within(list).getByText("Fake Person Three")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load more items" }));

    expect(await within(list).findByText("Fake Person Four")).toBeInTheDocument();
    expect(within(list).getByText("Fake Person Three")).toBeInTheDocument();
    expect(itemUrls()).toHaveLength(2);
    expect(itemUrls()[1]).toContain(`after=${FIRST_ITEM}`);
    expect(screen.queryByRole("button", { name: "Load more items" })).toBeNull();
  });
});
