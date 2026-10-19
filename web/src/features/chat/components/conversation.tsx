"use client";

import { TypingDots } from "@/features/chat/components/typing-dots";
import type { ChatMessage } from "@/features/chat/components/types";

/**
 * The transcript, oldest first. `role="log"` makes a screen reader announce
 * streamed text. The live turn is replaced by the final reply, never added to:
 * the final check may have rewritten the streamed text. While `working` and no
 * text has arrived yet, three dots stand where the reply will appear.
 */
export function Conversation({
  messages,
  live,
  working = false,
}: {
  messages: ChatMessage[];
  live: string;
  working?: boolean;
}) {
  return (
    <div
      role="log"
      aria-label="Conversation"
      aria-live="polite"
      className="flex flex-1 flex-col gap-3 py-4"
    >
      {messages.length === 0 && live.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Ask for the access you need, in your own words.
        </p>
      ) : null}

      {messages.map((message, index) => (
        <Message key={index} role={message.role} content={message.content} />
      ))}

      {live.length > 0 ? <Message role="assistant" content={live} /> : null}

      {working && live.length === 0 ? <TypingDots /> : null}
    </div>
  );
}

function Message({ role, content }: ChatMessage) {
  return (
    <article
      role="article"
      data-role={role}
      className={
        role === "user"
          ? "self-end rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground"
          : "self-start rounded-md bg-muted px-3 py-2 text-sm text-foreground"
      }
    >
      <span className="mb-1 block text-xs font-medium opacity-70">
        {role === "user" ? "You" : "Assistant"}
      </span>
      <p className="whitespace-pre-wrap">{content}</p>
    </article>
  );
}
