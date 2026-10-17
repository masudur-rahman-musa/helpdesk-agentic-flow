"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/field";
import { MAX_MESSAGE_CHARS, chatMessageSchema } from "@/features/chat/schema";

/**
 * The message box. It checks with the same `chatMessageSchema` the server
 * runs, so both agree on what is sendable, and a bad message opens no request.
 * No `maxLength`: a silent cap would hide the refusal, so the count is shown.
 * `initialValue` is a renewal ask from `/access`; the user still presses Send.
 * Nothing is ever disabled, so a failed turn never leaves the user stuck.
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
      <Label htmlFor="chat-message" className="sr-only">
        Message
      </Label>
      <Input
        id="chat-message"
        name="message"
        type="text"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        className="rounded-full px-4 py-3 shadow-sm"
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
