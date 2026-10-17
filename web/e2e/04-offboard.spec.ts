import { expect, test } from "@playwright/test";

import { accounts, loginAs, type Principal, sendTurn } from "./_fixtures";

/**
 * Flow 4: the HR demo account offboards a
 * leaver in chat, that user holds no active grant afterwards, and
 * `/admin/audit` shows one revoke event per grant that existed before the turn.
 *
 * The leaver is the `sod_employee` account, whose part in the suite finished
 * with flow 3 and whose credentials `accounts()` already carries — the count
 * of grants BEFORE the turn has to be read off a real page, and `/access` is
 * the only surface in the product that lists one person's grants, so the
 * leaver has to be someone who can sign in beforehand. Their template gives
 * them several rows, so the test ("a fixed count of 1 passes an
 * offboard that revoked only the first grant") has something to bite on; the
 * premise assertion below reads that off the page rather than assuming it.
 *
 * DEVIATION from reading that the user holds no active grant afterwards. There is
 * no surface on which anyone can read another person's grants: `/access` is
 * `GET /api/me/access`, there is no user-detail page, and the leaver cannot
 * sign in to read their own — `agent_api/auth_routes.py` refuses a leaver with
 * the same single 401 as a wrong password. The two reachable observations are
 * asserted instead, and together they are the criterion: exactly one
 * `grant.revoked` event per grant that existed, and the account itself refusing
 * to sign in afterwards.
 *
 * The audit page is read as `it_admin`, whose `search_audit` cell is
 * `Scope.ANY` (`helpdesk/auth/rbac.py`). Filtering on the event type alone is
 * enough because the suite runs serially against a database reset once before
 * it: flows 1 to 3 write `grant.created`, never a revoke,
 * and flow 5's revoke happens after this file.
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

/** `access/lifecycle.py::USER_OFFBOARDED_EVENT_TYPE`, verbatim. */
const USER_OFFBOARDED = "user.offboarded";

/**
 * Catches an offboard that revokes only the first of a person's grants, and
 * one that revokes rows without recording them: the expected count is read off
 * the leaver's own `/access` before the turn, so a loop that stops after one
 * row, and a hard-coded 1, both fail. It is not satisfied by the neighbouring
 * `user.offboarded` event either, which is asserted separately and counted on
 * its own.
 */
test("an HR offboard revokes every grant the leaver held and records one event each", async ({
  page,
}) => {
  const all = await accounts();
  const hr = who(all, "hr");
  const leaver = who(all, "sod_employee");
  const itAdmin = who(all, "it_admin");

  await loginAs(page, leaver);
  await page.goto("/access");
  await expect(page.getByRole("region", { name: "Active access" })).toBeVisible();
  const heldBefore = await page.getByRole("list", { name: "Grants" }).getByRole("listitem").count();
  // A premise, read off the page: with one grant the case could not tell a
  // complete offboard from one that revoked the first row and stopped.
  expect(heldBefore).toBeGreaterThan(1);

  await loginAs(page, hr);
  await page.goto("/chat");
  await sendTurn(page, `${leaver.displayName} (${leaver.email}) is leaving today, offboard them.`);

  await loginAs(page, itAdmin);
  await page.goto("/admin/audit");
  await expect(page.getByRole("region", { name: "Audit log" })).toBeVisible();

  // The rows of the results table, without its header row: `getByRole("row")`
  // on the table itself counts the `<thead>` row too, which would make every
  // count in this file one too many.
  const eventRows = page
    .getByRole("table", { name: "Audit events" })
    .locator("tbody")
    .getByRole("row");

  await page.getByRole("textbox", { name: "Event type" }).fill(GRANT_REVOKED);
  await page.getByRole("button", { name: "Search" }).click();
  await expect(eventRows).toHaveCount(heldBefore);

  await page.getByRole("textbox", { name: "Event type" }).fill(USER_OFFBOARDED);
  await page.getByRole("button", { name: "Search" }).click();
  await expect(eventRows).toHaveCount(1);
});

/**
 * Catches an offboard that revokes the grants but leaves the person's
 * directory status `active`: their credentials would still open a session, and
 * the next template sync or manual grant would hand a leaver access again. The
 * previous case cannot see this — it counts events, and every one of them
 * would still be written.
 */
test("the leaver can no longer sign in", async ({ page }) => {
  const leaver = who(await accounts(), "sod_employee");

  await page.goto("/login");
  await page.getByLabel("Tenant").fill(leaver.tenantSlug);
  await page.getByLabel("Email").fill(leaver.email);
  await page.getByLabel("Password").fill(leaver.password);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);
});
