import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

import { type ChatMessage, readHistory } from "@/features/chat/components/types";
import type { ChatMessageInput } from "@/features/chat/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/** The stored transcript, read through TanStack Query. */
export function useChatHistory() {
  return useQuery({ queryKey: queryKeys.chatMessages, queryFn: fetchHistory });
}

/**
 * Start one chat turn. Answers the raw response: its frames drive the panel's
 * own state, so reading the stream stays in `chat-panel.tsx`. A
 * dropped network throws, which the panel shows as `INTERNAL`.
 */
export async function postChatMessage(body: ChatMessageInput): Promise<Response> {
  return fetch(API.chatStream, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

/**
 * The stored transcript, or the empty one while the browser follows the proxy's
 * redirect to `/login`.
 *
 * `z.unknown()` and not a schema (24ia decision A): `readHistory` is what
 * validates this body and still is, so the parameter is only there to let the
 * redirect, the status and the typed code live in one module with the other
 * seven reads. Every non-ok response THROWS out of `readWire` — an empty
 * conversation and an unreadable one are different things, and Query is what
 * decides whether to retry (`tests/chat-page.test.tsx::a 403 FORBIDDEN is a
 * failed read, not an empty transcript, and raises no alert`).
 *
 * `null` is the redirect and, because `z.unknown()` accepts everything, also a
 * body that is literally `null` — the same empty transcript either way, which is
 * what `readHistory(null)` already answered. The one edge `z.unknown()` moves is
 * recorded in the plan's Deviations: a 200 carrying no JSON at all used to
 * reject here and now reads as an empty transcript, because this schema refuses
 * nothing.
 */
async function fetchHistory(): Promise<ChatMessage[]> {
  const body = await readWire(API.chatMessages, z.unknown());
  return body === null ? [] : readHistory(body);
}
