"use client";

import type { ChatMessage } from "@/features/chat/components/types";

/**
 * The transcript: history and this turn in one list, oldest first.
 *
 * `role="log"` with the accessible name "Conversation" — a log is the region a
 * screen reader announces additions to, which is what a streamed turn is. Each
 * message is an `<article>` carrying `data-role`, so a user's question and the
 * assistant's answer are distinguishable to a test and, through the visible
 * label, to a reader. Pinned by `tests/chat-page.test.tsx`'s "renders this
 * session's messages oldest first, users and assistants apart".
 *
 * The live turn is the LAST article and holds the tokens as they arrive. It is
 * replaced by the settled `reply`, never appended to it: the post-check may
 * have regenerated the text the tokens carried, and showing both
 * would leave the replaced composition standing beside the checked one.
 */
export function Conversation({ messages, live }: { messages: ChatMessage[]; live: string }) {
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
