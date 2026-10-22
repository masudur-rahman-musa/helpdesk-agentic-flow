import { useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import {
  type ChatMessage,
  type ChatSummary,
  readChatList,
  readHistory,
} from "@/features/chat/components/types";
import type { ChatMessageInput } from "@/features/chat/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { readWire } from "@/lib/wire-read";

/**
 * One chat's lines. Off while `chatId` is null (a new chat has none). Refetched
 * on every open, because messages sent since the last open are not in the cache.
 */
export function useChatHistory(chatId: string | null) {
  return useQuery({
    queryKey: queryKeys.chatMessages(chatId ?? ""),
    queryFn: () => fetchHistory(chatId ?? ""),
    enabled: chatId !== null,
    refetchOnMount: "always",
  });
}

/**
 * A function to run after each reply: it reloads the chat list, since a reply
 * may name a new chat, and the person's requests and tickets, so one made in
 * the turn shows at once.
 */
export function useRefreshAfterReply(): () => void {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: queryKeys.chatList });
    void client.invalidateQueries({ queryKey: queryKeys.myRequests });
    void client.invalidateQueries({ queryKey: queryKeys.myTickets });
  };
}

/** The person's chats, newest first. */
export function useChatList() {
  return useQuery({ queryKey: queryKeys.chatList, queryFn: fetchChatList });
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
async function fetchHistory(chatId: string): Promise<ChatMessage[]> {
  const url = `${API.chatMessages}?chat_id=${encodeURIComponent(chatId)}`;
  const body = await readWire(url, z.unknown());
  return body === null ? [] : readHistory(body);
}

/** Empty while redirecting to `/login`. A failed read throws, so the list shows an alert. */
async function fetchChatList(): Promise<ChatSummary[]> {
  const body = await readWire(API.chatList, z.unknown());
  return body === null ? [] : readChatList(body);
}
