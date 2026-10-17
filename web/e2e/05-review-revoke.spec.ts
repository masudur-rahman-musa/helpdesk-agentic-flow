import { expect, type Page, test } from "@playwright/test";

import { accounts, loginAs, type Principal } from "./_fixtures";

/**
 * Flow 5: the manager opens `/reviews`,
 * revokes a stale grant on an open review item, and the item then shows
 * revoked with the grant actually gone.
 *
 * The reviewer is `employee_manager`, the Production department's manager
 * (`scripts/seed.py` makes index 0 of every department its manager and
 * `_assign_principals` gives them the `manager` principal). Production is the
 * scope that has stale grants to find: `_generate_grants` expires the first
 * template row of every user whose `tenant_index % 20 == 7`, which inside
 * Production is indexes 21, 41 and 61, and `access/reviews.py::_flag_grants`
 * flags a lapsed-but-active grant `stale` — `REVIEW_FLAG_LABELS.stale` is
 * `Expiring with no renewal`.
 *
 * HARNESS REQUIREMENT (the e2e reset script). No review
 * exists after a seed: `scripts/seed.py` plants none, and the only writer is
 * `open_review`, driven by the scheduler, which the e2e run does not start as a
 * `webServer`. The reset therefore has to open one review over a scope that
 * contains a stale grant and whose reviewer is `employee_manager`, or this
 * flow has nothing to select.
 *
 * DEVIATION from completing the review. There is no such act in the
 * product. `web/lib/reviews-schema.ts` says it in as many words — "there is no
 * `review.completed` event and no `complete_review` tool" — and
 * `components/reviews/review-queue.tsx` carries no control for it: a review
 * completes when its LAST undecided item is decided, and `open_review` writes
 * "an item per live grant" in the scope, which for any seeded scope is dozens
 * to hundreds of clicks. Completion is therefore not asserted. What the flow really needs
 * is asserted instead, and more strictly than the
 * item's own label would: the `grant.revoked` count on `/admin/audit` before
 * and after the click. A review that records a decision and does nothing
 * leaves that count where it was.
 */

/** See the note in `01-auto-grant.spec.ts`; same reason, same shape. */
function who(all: Record<string, Principal>, key: string): Principal {
  const found = all[key];
  if (found === undefined) {
    throw new Error(`no e2e account named ${key}`);
  }
  return found;
}

/** `access/revoke.py::GRANT_REVOKED_EVENT_TYPE`, verbatim. */
const GRANT_REVOKED = "grant.revoked";

/** `web/lib/review-labels.ts::REVIEW_FLAG_LABELS.stale`, verbatim. */
const STALE_FLAG = "Expiring with no renewal";

/** `web/lib/review-labels.ts::REVIEW_DECISION_LABELS.revoke`, verbatim. */
const REVOKED_LABEL = "Revoked";

/** Every `grant.revoked` row the audit board answers for, as `it_admin`. */
async function revokedEventCount(page: Page, itAdmin: Principal): Promise<number> {
  await loginAs(page, itAdmin);
  await page.goto("/admin/audit");
  await expect(page.getByRole("region", { name: "Audit log" })).toBeVisible();
  await page.getByRole("textbox", { name: "Event type" }).fill(GRANT_REVOKED);
  await page.getByRole("button", { name: "Search" }).click();
  // Without `tbody` the header row counts too; see `04-offboard.spec.ts`.
  const rows = page.getByRole("table", { name: "Audit events" }).locator("tbody").getByRole("row");
  await expect(rows.first()).toBeVisible();
  return rows.count();
}

/**
 * Catches a `decide_item` that files the reviewer's word and never touches the
 * grant behind it: the item would still render `Revoked` and the review would
 * still progress, and only the audit count moves. It is not satisfied by the
 * offboard of flow 4 either — the count is taken inside this case, immediately
 * before the click.
 */
test("revoking a stale item on an open review revokes the grant behind it", async ({ page }) => {
  const all = await accounts();
  const manager = who(all, "employee_manager");
  const itAdmin = who(all, "it_admin");

  const revokedBefore = await revokedEventCount(page, itAdmin);

  await loginAs(page, manager);
  await page.goto("/reviews");
  await expect(page.getByRole("region", { name: "Reviews" })).toBeVisible();

  // The filter's own default is `open`, so the queue on paint
  // is the open reviews and nothing else. A premise, read off the page.
  await expect(page.getByRole("combobox", { name: "Status" })).toHaveValue("open");
  const queue = page.getByRole("list", { name: "Review queue" }).getByRole("listitem");
  await expect(queue.first()).toBeVisible();

  await page
    .getByRole("button", { name: /^Show items for / })
    .first()
    .click();

  const items = page.getByRole("list", { name: "Items in review" }).getByRole("listitem");
  const staleItem = items.filter({ hasText: STALE_FLAG }).first();
  await expect(staleItem).toBeVisible();

  const revoke = staleItem.getByRole("button", { name: /^Revoke / });
  await expect(revoke).toHaveCount(1);
  await revoke.click();

  // The controls are replaced by the decision label once the item carries one
  // (`review-queue.tsx::ItemRow`), so both halves are asserted: the word is
  // recorded, and no Keep/Revoke pair is offered a second time.
  await expect(staleItem).toContainText(REVOKED_LABEL);
  await expect(staleItem.getByRole("button", { name: /^Revoke / })).toHaveCount(0);
  await expect(staleItem.getByRole("button", { name: /^Keep / })).toHaveCount(0);

  const revokedAfter = await revokedEventCount(page, itAdmin);
  expect(revokedAfter).toBe(revokedBefore + 1);
});
