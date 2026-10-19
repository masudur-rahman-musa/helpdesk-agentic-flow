import { expect, type Page, test } from "@playwright/test";

import { accounts, loginAs, type Principal, sendTurn } from "./_fixtures";

/**
 * The unhappy path: with the `create_access_request` kill switch
 * set for the tenant, flow 1's ask is refused in the chat with the typed
 * message, and no access-request row is created by that turn.
 *
 * The switch is set and released through the shipped board
 * (`components/admin/kill-switch-toggle.tsx`) as `platform_admin`, the only
 * principal with a cell for `set_kill_switch` and `list_kill_switches`
 * (`helpdesk/auth/rbac.py`). It is released in an `afterAll` as well
 * as at the end of the case, so a failed run does not leave the tenant's
 * request path stopped for the next one.
 *
 * The refusal sentence is `FAILURE_MESSAGES.TOOL_DISABLED` from
 * `web/lib/chat-errors.ts`, asserted verbatim and exact. `TOOL_DISABLED` and
 * not `FORBIDDEN`: `helpdesk/errors.py::ToolDisabledError` exists precisely so
 * a stopped tool is tellable from a denied principal, and asserting only "an
 * alert appeared" would pass for either.
 *
 * The row half is a BEFORE/AFTER count of `/requests`, not an empty-state
 * assertion: the same employee already filed two requests in flows 1 and 2, so
 * "the list is empty" is false whatever the switch does.
 */

/** See the note in `01-auto-grant.spec.ts`; same reason, same shape. */
function who(all: Record<string, Principal>, key: string): Principal {
  const found = all[key];
  if (found === undefined) {
    throw new Error(`no e2e account named ${key}`);
  }
  return found;
}

/** The tool this case stops, as `rbac.Tool.CREATE_ACCESS_REQUEST.value`. */
const TOOL = "create_access_request";

/** `web/lib/chat-errors.ts::FAILURE_MESSAGES.TOOL_DISABLED`, verbatim. */
const TOOL_DISABLED_MESSAGE = "That is switched off right now. Try again later.";

/** `roles.name` as `scripts/seed.py` writes it: `{system}.{slug}`. */
const WIKI_VIEWER = "wiki.viewer";

/** The board, with the row for this tool already on screen. */
async function openBoard(page: Page, admin: Principal): Promise<void> {
  await loginAs(page, admin);
  await page.goto("/admin/killswitch");
  await expect(page.getByRole("region", { name: "Kill switches" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Tool kill switches" })).toBeVisible();
}

/** Releases this tenant's switch if it is holding one; a no-op otherwise. */
async function releaseTenantSwitch(page: Page, admin: Principal): Promise<void> {
  await openBoard(page, admin);
  const release = page.getByRole("button", { name: `Release for this tenant for ${TOOL}` });
  if ((await release.count()) === 0) {
    return;
  }
  await page.getByRole("textbox", { name: `Reason for ${TOOL}` }).fill("End of the e2e run.");
  await release.click();
  await expect(
    page.getByRole("button", { name: `Stop for this tenant for ${TOOL}` }),
  ).toBeVisible();
}

test.afterAll(async ({ browser }) => {
  const page = await browser.newPage();
  try {
    await releaseTenantSwitch(page, who(await accounts(), "platform_admin"));
  } finally {
    await page.close();
  }
});

/**
 * Catches a kill switch that refuses the REPLY after the tool has already
 * written its row: the sentence alone would pass that, and the request count
 * is what does not. It also catches a switch wired to the wrong error class —
 * a `FORBIDDEN` refusal renders "You are not allowed to do that." and fails the
 * exact-sentence assertion while still showing an alert.
 */
test("a stopped create_access_request refuses the turn and writes no request row", async ({
  page,
}) => {
  const all = await accounts();
  const admin = who(all, "platform_admin");
  const employee = who(all, "employee");

  await openBoard(page, admin);
  await page.getByRole("textbox", { name: `Reason for ${TOOL}` }).fill("Plan 25 unhappy path.");
  await page.getByRole("button", { name: `Stop for this tenant for ${TOOL}` }).click();
  // The board re-reads after the change, and a row holding a tenant switch
  // offers the Release rather than the Stop. Waiting on that is what makes the
  // chat turn below race-free.
  await expect(
    page.getByRole("button", { name: `Release for this tenant for ${TOOL}` }),
  ).toBeVisible();

  await loginAs(page, employee);
  const requestRows = page.getByRole("list", { name: "Requests" }).getByRole("listitem");
  await page.goto("/requests");
  await expect(page.getByRole("region", { name: "My requests" })).toBeVisible();
  const rowsBefore = await requestRows.count();

  await page.goto("/chat");
  await sendTurn(page, `Please give me ${WIKI_VIEWER} access to the internal wiki.`);
  // The assistant first asks "Do you mean ...?"; only the yes files it.
  await sendTurn(page, "Yes, that is right.");

  await expect(page.getByRole("alert")).toHaveText(TOOL_DISABLED_MESSAGE);
  // A refused turn decides nothing, so there is no card to render
  // (`chat-panel.tsx` clears the decision on an error frame).
  await expect(page.getByRole("region", { name: "Decision" })).toHaveCount(0);

  await page.goto("/requests");
  await expect(page.getByRole("region", { name: "My requests" })).toBeVisible();
  await expect(requestRows).toHaveCount(rowsBefore);

  await releaseTenantSwitch(page, admin);
});
