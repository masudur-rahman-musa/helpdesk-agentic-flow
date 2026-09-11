/**
 * The renewal ask `/access` writes into a link and `/chat` reads back out of
 * the address bar.
 *
 * Two pure functions and one name, so neither end needs a router: the panel
 * hands `window.location.search` to `readAsk` on mount, which keeps
 * `useSearchParams` — and the Suspense boundary it obliges — out of
 * `tests/chat-page.test.tsx` entirely (§2).
 *
 * `readAsk` refuses through `chatMessageSchema`, the SAME pre-check the
 * composer and the route handler run: a link carrying an empty or oversized
 * ask leaves the composer empty rather than seeding a message the app would
 * then refuse to send (`tests/chat-prefill.test.ts`'s refusal rows and its
 * inclusive boundary at `MAX_MESSAGE_CHARS`, and `tests/chat-page.test.tsx`'s
 * `starts empty with ...` rows).
 *
 * The query string is read with `URLSearchParams` and written with
 * `encodeURIComponent`, never by hand: a role called "Fake Payroll Read &
 * Write" is exactly the value a hand-split reader truncates at its ampersand,
 * pinned as a round trip by `::carries a sentence naming the role and the
 * system, encoded so readAsk gets it whole`.
 *
 * Nothing here sends anything. The link fills the composer and the user still
 * presses Send, which is what `::starts the composer carrying the ask and
 * sends nothing until Send is pressed` asserts with its call count.
 */

import type { MyGrant } from "@/lib/access-schema";
import { chatMessageSchema } from "@/lib/chat-schema";

/** The one parameter name, shared by the link, the reader and the design's URL. */
export const CHAT_PREFILL_PARAM = "ask";

/** The prefill of a `window.location.search`, or null if there is none to use. */
export function readAsk(search: string): string | null {
  const ask = new URLSearchParams(search).get(CHAT_PREFILL_PARAM);
  if (ask === null) {
    return null;
  }
  const parsed = chatMessageSchema.safeParse({ message: ask });
  return parsed.success ? parsed.data.message : null;
}

/** The `/chat` link that asks to renew one grant, role and system named. */
export function renewalHref(grant: MyGrant): string {
  const ask = `Can I renew my ${grant.role_name} access on ${grant.system_name}?`;
  return `/chat?${CHAT_PREFILL_PARAM}=${encodeURIComponent(ask)}`;
}
