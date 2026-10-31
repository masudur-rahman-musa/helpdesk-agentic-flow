"use client";

import { TypingDots } from "@/features/chat/components/typing-dots";
import type { ChatMessage } from "@/features/chat/components/types";
import { FormattedText } from "@/features/chat/format";

/**
 * The transcript, oldest first. `role="log"` makes a screen reader announce
 * streamed text. The live turn is replaced by the final reply, never added to:
 * the final check may have rewritten the streamed text. While `working` and no
 * text has arrived yet, three dots stand where the reply will appear, with the
 * newest `progress` words beside them.
 * Assistant replies draw their lists and bold; the person's own messages stay
 * exactly as typed.
 */
export function Conversation({
  messages,
  live,
  working = false,
  progress = "",
}: {
  messages: ChatMessage[];
  live: string;
  working?: boolean;
  progress?: string;
}) {
  return (
    <div
      role="log"
      aria-label="Conversation"
      aria-live="polite"
      className="flex flex-col gap-3 py-4"
    >
      {messages.length === 0 && live.length === 0 ? (
        <div className="flex flex-col gap-1">
          <h2 className="text-base font-semibold">Access and IT help</h2>
          <p className="text-sm text-muted-foreground">
            Ask here to request or remove access, check what access and requests you have, reset
            your password or unlock your account, open or check a ticket, or ask about the access
            policy. For anything else, ask me to open a ticket and a person will pick it up.
          </p>
        </div>
      ) : null}

      {messages.map((message, index) => (
        <Message key={index} role={message.role} content={message.content} />
      ))}

      {live.length > 0 ? <Message role="assistant" content={live} /> : null}

      {working && live.length === 0 ? <TypingDots words={progress} /> : null}
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
      {role === "user" ? (
        <p className="whitespace-pre-wrap">{content}</p>
      ) : (
        <div className="flex flex-col gap-2">
          <FormattedText text={content} />
        </div>
      )}
    </article>
  );
}
