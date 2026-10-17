import { expect, test } from "@playwright/test";

import { accounts, decisionCard, loginAs, type Principal, sendTurn } from "./_fixtures";

/**
 * Flow 3: an employee who already holds one
 * half of a seeded SoD pair asks for the other half and is blocked.
 *
 * The pair is `scripts/seed.py::NORDLICHT.sod_rules`' first rule, `SoD-STD
 * 2.1`: `payments.initiator` against `payments.approver`. The actor holds
 * `payments.approver` from `NORDLICHT.planted_conflicts` and asks for
 * `payments.initiator`.
 *
 * DEVIATION from the card naming the rule that stopped it. No
 * surface in the product names it. `web/lib/decision-labels.ts::reasonSentence`
 * splits `SOD_CONFLICT:{rule_id}` on the colon and renders only
 * `REASON_SENTENCES.SOD_CONFLICT`, deliberately (a raw
 * uuid reads as nothing to the person asking), and the decision card, the
 * `/requests` rows and the `/approvals` rows all render through that one
 * function. The e2e work must not touch `web/components`, so the card is
 * asserted for what it CAN say — `Blocked` plus the conflict sentence — and
 * the rule is then shown to be nameable where it belongs, on `/admin/rules`
 * (`components/admin/rules-board.tsx`, region `Rules`, table `Separation of
 * duties rules`), whose audience is `it_admin` and `platform_admin`
 * (`helpdesk/auth/rbac.py::Tool.LIST_SOD_RULES`). The kill claim
 * stays honest: the card alone cannot say why.
 *
 * The rules-board half is a separate `test()` because it pins a different
 * control (the read tool and its board) and would otherwise ride on the
 * chat turn's red.
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
const PAYMENTS_INITIATOR = "payments.initiator";
const PAYMENTS_APPROVER = "payments.approver";

/** `SodRuleSpec.policy_ref` of `NORDLICHT.sod_rules[0]`, verbatim. */
const SOD_RULE_REF = "SoD-STD §2.1";

/**
 * Catches an implementation that lets the second half of a seeded SoD pair
 * through, and one that blocks it for the wrong reason: the exact reason
 * sentence is `REASON_SENTENCES.SOD_CONFLICT`, so a `POLICY_BLOCKED` or a
 * not-in-template refusal renders a different sentence and fails here even
 * though the card still reads `Blocked`.
 */
test("asking for the other half of a seeded SoD pair is blocked, with the conflict named", async ({
  page,
}) => {
  const sodEmployee = who(await accounts(), "sod_employee");
  await loginAs(page, sodEmployee);

  // Counted before and after rather than asserted absent: depending on which
  // `planted_conflicts` entry the harness picks as `sod_employee`, the actor's
  // department template may ALREADY carry `payments.initiator` (the Finance
  // template does; the Procurement one does not), and `create_access_request`
  // has no duplicate-grant refusal. A blocked ask must add nothing either way.
  const grants = page.getByRole("list", { name: "Grants" }).getByRole("listitem");
  await page.goto("/access");
  await expect(page.getByRole("region", { name: "Active access" })).toBeVisible();
  const initiatorRowsBefore = await grants.filter({ hasText: PAYMENTS_INITIATOR }).count();

  await page.goto("/chat");
  await sendTurn(page, `Please give me ${PAYMENTS_INITIATOR} so I can raise payment runs.`);

  // `DECISION_LABELS.blocked` and `STATUS_LABELS.rejected`
  // (`_STATUS_FOR_DECISION[BLOCKED]` is `rejected`).
  const card = decisionCard(page);
  await expect(card).toContainText("Blocked");
  await expect(card).toContainText("Rejected");

  // The reasons `<ul>` inside the region carries no accessible name and is the
  // FIRST list in it (`decision-card.tsx` renders reasons above `Approvers`).
  // The sentence is `REASON_SENTENCES.SOD_CONFLICT`, verbatim: this is the
  // whole of what the card can say about the rule.
  const reasons = card.getByRole("list").first();
  await expect(
    reasons.getByRole("listitem").filter({
      hasText: "This would conflict with access you already hold.",
    }),
  ).toHaveCount(1);

  // A blocked ask writes no grant.
  await page.goto("/access");
  await expect(page.getByRole("region", { name: "Active access" })).toBeVisible();
  await expect(grants.filter({ hasText: PAYMENTS_INITIATOR })).toHaveCount(initiatorRowsBefore);
});

/**
 * Catches a product in which the rule that blocked the ask is nameable
 * NOWHERE: with the card stripped of the rule id by design, `/admin/rules` is
 * the only surface that pairs the two roles with the policy reference, and an
 * empty or partial board would leave an admin unable to answer "which rule was
 * that". Scoped to the row carrying `SoD-STD 2.1` so the neighbouring rules
 * that also mention `payments.approver` or `payments.initiator`
 * cannot satisfy it.
 */
test("the rule that blocked it is named on the admin rules board", async ({ page }) => {
  const itAdmin = who(await accounts(), "it_admin");
  await loginAs(page, itAdmin);
  await page.goto("/admin/rules");

  await expect(page.getByRole("region", { name: "Rules" })).toBeVisible();
  const ruleRow = page
    .getByRole("table", { name: "Separation of duties rules" })
    .getByRole("row")
    .filter({ hasText: SOD_RULE_REF });
  await expect(ruleRow).toHaveCount(1);
  await expect(ruleRow).toContainText(PAYMENTS_INITIATOR);
  await expect(ruleRow).toContainText(PAYMENTS_APPROVER);
});
