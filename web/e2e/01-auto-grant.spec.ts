import { expect, test } from "@playwright/test";

import { accounts, decisionCard, loginAs, type Principal, sendTurn, sources } from "./_fixtures";

/**
 * Flow 1: the demo employee asks for a role
 * their department template already covers, the card reads granted, at least
 * one citation is listed under Sources, and the role appears on `/access`.
 *
 * The ask is `wiki.viewer` — the Production template's one role that is LOW
 * under every branch of `helpdesk/policy/risk.py` (system `wiki` is
 * sensitivity `low`, the role's privilege is `read`, and it is in template),
 * so `decision.py` answers `auto` with an EMPTY approver chain and
 * `_STATUS_FOR_DECISION[AUTO]` files the request as `executed`.
 *
 * Every selector here is a role or an `aria-label` the shipped UI already
 * carries: `Decision` and `Approvers` from
 * `components/chat/decision-card.tsx`, `Grants` from
 * `components/access/grant-list.tsx`, and the Sources section by its heading —
 * see `_fixtures.ts::sources` and the DEVIATION recorded in
 * `03-sod-blocked.spec.ts` for why that one is not a named region.
 *
 * The `/access` assertion is a BEFORE/AFTER count and never a date, because
 * every user already holds every role of their department template from the
 * seed (`scripts/seed.py::_generate_grants` adds one grant per template row):
 * "a `wiki.viewer` row exists" is true before the turn and would pass against
 * an implementation that granted nothing at all. Counting rows also survives a
 * run that crosses UTC midnight, which a `Granted {today}` assertion would not.
 */

/**
 * One named account out of the harness map.
 *
 * Local rather than imported: `accounts()` answers a
 * `Record<string, Principal>`, and `noUncheckedIndexedAccess` in
 * `web/tsconfig.json` makes every lookup `Principal | undefined`. A missing key
 * has to fail with the key's name, not as `Cannot read properties of undefined`
 * inside `loginAs`.
 */
function who(all: Record<string, Principal>, key: string): Principal {
  const found = all[key];
  if (found === undefined) {
    throw new Error(`no e2e account named ${key}`);
  }
  return found;
}

/** `roles.name` as `scripts/seed.py` writes it: `{system}.{slug}`. */
const WIKI_VIEWER = "wiki.viewer";

/**
 * Catches an implementation that renders a granted card without writing the
 * grant, or that grants without retrieving any policy: the `/access` count
 * would stay where it was, and the Sources list would be empty.
 */
test("an in-template ask is granted on the spot, cites policy, and lands on /access", async ({
  page,
}) => {
  const employee = who(await accounts(), "employee");
  await loginAs(page, employee);

  const grants = page.getByRole("list", { name: "Grants" }).getByRole("listitem");
  await page.goto("/access");
  await expect(page.getByRole("region", { name: "Active access" })).toBeVisible();
  const wikiRowsBefore = await grants.filter({ hasText: WIKI_VIEWER }).count();

  await page.goto("/chat");
  await sendTurn(page, `Please give me ${WIKI_VIEWER} access to the internal wiki.`);

  // `DECISION_LABELS.auto`, `RISK_LABELS.low` and `STATUS_LABELS.executed`
  // from `web/lib/decision-labels.ts`, verbatim. Asserting the risk beside the
  // decision is what separates "the engine classified this LOW and therefore
  // auto-granted it" from "something auto-granted a privileged ask".
  const card = decisionCard(page);
  await expect(card).toContainText("Granted automatically");
  await expect(card).toContainText("Low risk");
  await expect(card).toContainText("Access granted");

  // `_base_chain(LOW)` is `()`. A card that named an approver for a LOW auto
  // grant would mean the chain and the decision disagree.
  await expect(card.getByRole("list", { name: "Approvers" }).getByRole("listitem")).toHaveCount(0);

  // The check that matters here: delete this and a grant with no policy
  // behind it passes.
  await expect(sources(page).getByRole("listitem").first()).toBeVisible();

  await page.goto("/access");
  await expect(grants.filter({ hasText: WIKI_VIEWER })).toHaveCount(wikiRowsBefore + 1);
});
