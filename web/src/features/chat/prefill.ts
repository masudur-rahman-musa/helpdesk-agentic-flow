/**
 * The renewal ask `/access` writes into a link and `/chat` reads back. It is
 * checked with the same schema the composer uses, so a bad link leaves the
 * box empty. Nothing here sends: the user still presses Send.
 */

import type { MyGrant } from "@/features/access/schema";
import { chatMessageSchema } from "@/features/chat/schema";

export const CHAT_PREFILL_PARAM = "ask";

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
