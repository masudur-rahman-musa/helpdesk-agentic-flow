import { readFile } from "node:fs/promises";

import { expect, type Locator, type Page } from "@playwright/test";

/**
 * The shared helpers of the e2e flows: who to sign in as, how to send a
 * turn, and the two locators worth naming once.
 *
 * Every selector here is a role or an `aria-label` the shipped UI already
 * carries. Nothing in `web/app/` or `web/components/` is touched
 * to make a flow pass, and a flow that cannot find its element is a finding
 * about the UI's accessibility rather than a reason to reach for a CSS
 * selector.
 */

/** One seeded person, exactly as the e2e accounts script prints them. */
export interface Principal {
  email: string;
  password: string;
  tenantSlug: string;
  displayName: string;
  department: string;
  userId: string;
}

/** The tenant the flows run in. `cuan` exists for the isolation tests. */
export const NORDLICHT = "nordlicht";

let cached: Record<string, Principal> | null = null;

/**
 * The seeded accounts, read once per worker from `E2E_ACCOUNTS_FILE`.
 *
 * A file rather than an env var per person, and derived rather than written
 * down: the e2e accounts script re-derives every password from the seed's
 * own formula, so no password string exists in a spec, a fixture or
 * `.env.example`.
 */
export async function accounts(): Promise<Record<string, Principal>> {
  if (cached !== null) {
    return cached;
  }
  const path = process.env.E2E_ACCOUNTS_FILE;
  if (path === undefined || path === "") {
    throw new Error("E2E_ACCOUNTS_FILE is not set; run `make e2e` rather than `playwright test`");
  }
  cached = JSON.parse(await readFile(path, "utf8")) as Record<string, Principal>;
  return cached;
}

/**
 * Sign in as one principal and land on `/chat`.
 *
 * The three fields are found by their labels and the button by its name, which
 * is how a person using a screen reader signs in too. Waiting for `/chat`
 * rather than for the form to disappear: a failed login re-renders the same
 * form with an alert, and a helper that returned there would fail the caller's
 * FIRST assertion instead of this line.
 */
export async function loginAs(page: Page, principal: Principal): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Tenant").fill(principal.tenantSlug);
  await page.getByLabel("Email").fill(principal.email);
  await page.getByLabel("Password").fill(principal.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL("**/chat");
}

/**
 * Send one chat turn and wait for it to settle.
 *
 * Settled means the assistant's reply has landed in the `Conversation` log or
 * the turn failed into the panel's alert. The composer has no pending state to
 * wait on, and the reply arrives over SSE, so counting the log's articles
 * before and after is the observation actually available: `role="log"` with
 * `role="article"` children is what `components/chat/conversation.tsx`
 * renders.
 *
 * The alert is raced rather than ignored on purpose. Flow 6's whole assertion
 * is a turn that produces a refusal and no reply, and a helper that waited for
 * an assistant article would sit there until the test timed out and report the
 * wrong failure.
 */
export async function sendTurn(page: Page, message: string): Promise<void> {
  const log = page.getByRole("log", { name: "Conversation" });
  const articles = log.getByRole("article");
  const before = await articles.count();

  await page.getByLabel("Message").fill(message);
  await page.getByRole("button", { name: "Send" }).click();

  // The user's own message lands immediately; the assistant's is the second.
  await expect
    .poll(async () => (await articles.count()) >= before + 2 || (await alert(page).count()) > 0, {
      message: "the turn produced neither an assistant reply nor a failure",
    })
    .toBe(true);
}

/** The chat panel's failure region, the one place a typed code is dressed. */
export function alert(page: Page): Locator {
  return page.getByRole("alert");
}

/** The turn's decision card: `aria-label="Decision"` on a `section`. */
export function decisionCard(page: Page): Locator {
  return page.getByRole("region", { name: "Decision" });
}

/**
 * The citations section of the current turn.
 *
 * NOT `getByRole("region", { name: "Sources" })`. `components/chat/citations.tsx`
 * renders an UNNAMED `<section>` whose first child is `<h2>Sources</h2>`, and a
 * `section` with no accessible name is not exposed as a `region` at all, so the
 * obvious query matches nothing. Located by the heading instead, and noted
 * here: `Sources` is a heading on this surface, not a label.
 */
export function sources(page: Page): Locator {
  return page.locator("section").filter({ has: page.getByRole("heading", { name: "Sources" }) });
}
