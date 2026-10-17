import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

import { type ChatMessage, readHistory } from "@/features/chat/components/types";
import type { ChatMessageInput } from "@/features/chat/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

export function useChatHistory() {
  return useQuery({ queryKey: queryKeys.chatMessages, queryFn: fetchHistory });
}

/** Returns the raw response; the panel reads the stream. A network drop throws. */
export async function postChatMessage(body: ChatMessageInput): Promise<Response> {
  return fetch(API.chatStream, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

/**
 * Empty while redirecting to `/login`. `readHistory` does the checking, so the
 * schema here accepts anything. A failed read throws: an unreadable chat is not
 * an empty one.
 */
async function fetchHistory(): Promise<ChatMessage[]> {
  const body = await readWire(API.chatMessages, z.unknown());
  return body === null ? [] : readHistory(body);
}
