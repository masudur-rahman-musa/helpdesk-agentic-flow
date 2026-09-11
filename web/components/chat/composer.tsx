"use client";

import { useState } from "react";

import { Button, Label } from "@/components/ui/field";
import { MAX_MESSAGE_CHARS, chatMessageSchema } from "@/lib/chat-schema";

/**
 * The message box. Its accessible name is "Message" and its button's is
 * "Send"; both are fixed by an earlier change §6 and queried by those names in
 * `tests/chat-page.test.tsx`.
 *
 * The pre-check is the SAME `chatMessageSchema` the route handler runs, so the
 * sentence a user sees here and the refusal the server would answer cannot
 * disagree about what is sendable. It runs before `onSend`, so an empty or
 * oversized message opens no request at all.
 *
 * There is no `maxLength` on the textarea: capping the input silently would
 * make the refusal unreachable and the counter a decoration. The count is
 * shown instead, and going over is refused with the same typed code the server
 * uses.
 *
 * `initialValue` is the ONE thing the panel may put in the box: the ask a
 * `/access` renewal link carried. It seeds the state and
 * nothing else — the user still presses Send — and it is already past
 * `chatMessageSchema` by the time it arrives, because the panel refuses an
 * empty or oversized parameter before rendering
 * (`tests/chat-page.test.tsx`'s `ChatPanel — the renewal prefill` block).
 *
 * Neither control is ever disabled — not while a turn streams, and not after
 * one fails (§1's unhappy paths). A turn that failed and locked the composer
 * leaves the user with nothing to do but reload; overlapping turns are
 * prevented by the panel's own re-entry guard, which costs the user nothing
 * they can see.
 */
export function Composer({
  onSend,
  onRefuse,
  initialValue = "",
}: {
  onSend: (message: string) => void;
  onRefuse: (code: string) => void;
  initialValue?: string;
}) {
  const [value, setValue] = useState(initialValue);

  function submit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const parsed = chatMessageSchema.safeParse({ message: value });
    if (!parsed.success) {
      onRefuse("VALIDATION");
      return;
    }
    setValue("");
    onSend(parsed.data.message);
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <Label htmlFor="chat-message">Message</Label>
      <textarea
        id="chat-message"
        name="message"
        rows={3}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      <div className="flex items-center justify-between gap-4">
        <span className="text-xs text-muted-foreground">
          {value.length} / {MAX_MESSAGE_CHARS}
        </span>
        <Button type="submit">Send</Button>
      </div>
    </form>
  );
}
