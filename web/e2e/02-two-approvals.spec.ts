import { expect, test } from "@playwright/test";

import { accounts, decisionCard, loginAs, type Principal, sendTurn } from "./_fixtures";

/**
 * Flow 2: the same employee asks for an admin
 * role, the card reads queued and names two approvers, the department manager
 * approves and then the system owner does, and the grant lands with an expiry
 * exactly 30 days after the grant date.
 *
 * The ask is `gitlab.maintainer`: system `gitlab` is sensitivity `medium` and
 * category `other`, the role's privilege is `admin`, and it is NOT in the
 * Production template, so `risk.py` answers PRIVILEGED (the `admin` row wins
 * over the not-in-template `elevated` row) and `decision.py::_base_chain`
 * gives exactly two slots, `department_manager` then `system_owner`. Neither
 * seat is a party to the request — the requester is Production index 5 and the
 * two seats are the Production manager and GitLab's owner in IT — so
 * `_substitute_parties` replaces nothing and the chain stays two long.
 *
 * `expiry.py::ADMIN_CAP_DAYS` is 30 and `CLASS_CAP_DAYS[PRIVILEGED]` is 90, so
 * the effective expiry is 30.
 *
 * DEVIATION from asserting the expiry date is exactly 30 days after the
 * grant date on the card. The DECISION CARD cannot carry that date: the card's
 * `expires_at` comes from `agent_api/decision_card.py::_card`, which reads it
 * off the created grant and answers `None` for a request that is only PENDING,
 * so a queued card renders `Expires in 30 days` with no `(until ...)` tail at
 * all. The date is observable after execution on two surfaces, and this spec
 * reads both and does the arithmetic between them: `Granted {date}` on
 * `/access` (`components/access/grant-list.tsx`) and `Expires {date}` on
 * `/requests` (`components/requests/request-list.tsx`, rendered only once the
 * request has a grant). Reading BOTH dates off the page rather than comparing
 * either against `new Date()` is also what keeps the case honest across a run
 * that crosses UTC midnight.
 *
 * The `/access` countdown reads `29 days left`, not 30:
 * `access/execute.py` sets `expires_at = now + timedelta(days=30)` and
 * `web/lib/expiry.ts::daysUntil` floors, so any read after that instant is 29
 * whole days. Pinned exactly rather than as a band, because 89 or a blank is
 * what a wrong cap or a null expiry would render.
 */

/** See the note in `01-auto-grant.spec.ts`; same reason, same shape. */
function who(all: Record<string, Principal>, key: string): Principal {
  const found = all[key];
  if (found === undefined) {
    throw new Error(`no e2e account named ${key}`);
  }
  return found;
}

/** `roles.name` as `scripts/seed.py` writes it: `{system}.{slug}`. */
const GITLAB_MAINTAINER = "gitlab.maintainer";

/** The ISO day `n` days after an ISO day, in UTC, as the app renders dates. */
function isoDayPlus(isoDay: string, days: number): string {
  const moved = new Date(`${isoDay}T00:00:00Z`);
  moved.setUTCDate(moved.getUTCDate() + days);
  return moved.toISOString().slice(0, 10);
}

/** The one ISO day in a rendered row, after a given word. */
function isoDayAfter(word: string, text: string): string {
  const found = new RegExp(`${word} (\\d{4}-\\d{2}-\\d{2})`).exec(text);
  if (found === null || found[1] === undefined) {
    throw new Error(`no "${word} YYYY-MM-DD" in: ${text}`);
  }
  return found[1];
}

/**
 * Catches an implementation that queues an admin ask behind ONE approver, that
 * executes the grant before the chain is complete, or that grants it uncapped:
 * a one-approver chain fails the count of two, a grant written after the first
 * approval fails because `/access` is read between the two approvals, and a
 * 90-day or standing grant fails the 30-day arithmetic.
 */
test("an admin ask needs two approvals and is granted for exactly 30 days", async ({ page }) => {
  const all = await accounts();
  const employee = who(all, "employee");
  const manager = who(all, "employee_manager");
  const owner = who(all, "gitlab_owner");

  await loginAs(page, employee);
  await page.goto("/chat");
  await sendTurn(page, `I need ${GITLAB_MAINTAINER} admin rights on GitLab for a migration.`);
  // The assistant first asks "Do you mean ...?"; only the yes files it.
  await sendTurn(page, "Yes, that is right.");

  // `DECISION_LABELS.approval`, `RISK_LABELS.privileged` and
  // `STATUS_LABELS.pending` from `web/lib/decision-labels.ts`, verbatim.
  const card = decisionCard(page);
  await expect(card).toContainText("Needs approval");
  await expect(card).toContainText("Privileged");
  await expect(card).toContainText("Pending approval");
  await expect(card).toContainText("Expires in 30 days");

  // Exactly two slots, in `_base_chain`'s order, both resolved to a person.
  // `SLOT_LABELS.department_manager` and `SLOT_LABELS.system_owner`, plus
  // `decision-card.tsx`'s `assigned`.
  const approvers = card.getByRole("list", { name: "Approvers" }).getByRole("listitem");
  await expect(approvers).toHaveCount(2);
  await expect(approvers.nth(0)).toContainText("Department manager");
  await expect(approvers.nth(0)).toContainText("assigned");
  await expect(approvers.nth(1)).toContainText("System owner");
  await expect(approvers.nth(1)).toContainText("assigned");

  const grants = page.getByRole("list", { name: "Grants" }).getByRole("listitem");

  await loginAs(page, manager);
  await page.goto("/approvals");
  await expect(page.getByRole("region", { name: "Approvals" })).toBeVisible();
  await page.getByRole("textbox", { name: `Comment for ${GITLAB_MAINTAINER}` }).fill("Approved.");
  await page.getByRole("button", { name: `Approve ${GITLAB_MAINTAINER}` }).click();
  await expect(
    page
      .getByRole("list", { name: "Pending approvals" })
      .getByRole("listitem")
      .filter({ hasText: GITLAB_MAINTAINER }),
  ).toHaveCount(0);

  // Read between the two approvals: an implementation that executes on the
  // first seat's decision fails here and nowhere else.
  await loginAs(page, employee);
  await page.goto("/access");
  await expect(page.getByRole("region", { name: "Active access" })).toBeVisible();
  await expect(grants.filter({ hasText: GITLAB_MAINTAINER })).toHaveCount(0);

  await loginAs(page, owner);
  await page.goto("/approvals");
  await expect(page.getByRole("region", { name: "Approvals" })).toBeVisible();
  await page.getByRole("textbox", { name: `Comment for ${GITLAB_MAINTAINER}` }).fill("Approved.");
  await page.getByRole("button", { name: `Approve ${GITLAB_MAINTAINER}` }).click();
  await expect(
    page
      .getByRole("list", { name: "Pending approvals" })
      .getByRole("listitem")
      .filter({ hasText: GITLAB_MAINTAINER }),
  ).toHaveCount(0);

  await loginAs(page, employee);
  await page.goto("/access");
  const grantRow = grants.filter({ hasText: GITLAB_MAINTAINER });
  await expect(grantRow).toHaveCount(1);
  await expect(grantRow).toContainText("29 days left");
  const grantedOn = isoDayAfter("Granted", (await grantRow.innerText()).trim());

  await page.goto("/requests");
  const requestRow = page
    .getByRole("list", { name: "Requests" })
    .getByRole("listitem")
    .filter({ hasText: GITLAB_MAINTAINER });
  await expect(requestRow).toHaveCount(1);
  // `STATUS_LABELS.executed`, not `approved`: the chain completing runs the
  // grant, and `_STATUS_FOR_DECISION` never leaves an executed request on
  // `approved`.
  await expect(requestRow).toContainText("Access granted");
  const expiresOn = isoDayAfter("Expires", (await requestRow.innerText()).trim());

  expect(expiresOn).toBe(isoDayPlus(grantedOn, 30));
});
