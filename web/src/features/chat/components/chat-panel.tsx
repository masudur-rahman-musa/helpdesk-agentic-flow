"use client";

import { useEffect, useRef, useState } from "react";

import { useMe } from "@/features/auth/api";
import { postChatMessage, useChatHistory } from "@/features/chat/api";
import { Citations } from "@/features/chat/components/citations";
import { Composer } from "@/features/chat/components/composer";
import { Conversation } from "@/features/chat/components/conversation";
import { DecisionCard } from "@/features/chat/components/decision-card";
import { ToolSteps } from "@/features/chat/components/tool-steps";
import {
  type ChatMessage,
  type Citation,
  type DecisionCard as DecisionCardData,
  type StepEvent,
  readCode,
  readReply,
  readStep,
  readToken,
} from "@/features/chat/components/types";
import { failureMessage } from "@/lib/failure-messages";
import { readAsk } from "@/features/chat/prefill";
import { chatMessageSchema } from "@/features/chat/schema";
import { createSseParser } from "@/lib/sse";
import { Alert } from "@/components/ui/alert";

/**
 * The chat page. History is a cached query; the live turn is local state, since
 * a stream has no cache key. The decision card, steps and citations belong to
 * the latest turn and clear on a new send or an error. The steps sit in a column
 * beside the chat, level with the composer, and drop under it on a narrow
 * screen. The composer starts with
 * `?ask=` from a renewal link but never sends on its own.
 */
export function ChatPanel() {
  const history = useChatHistory();
  // Steps show only when a platform admin turned them on; until `me` answers, they stay hidden.
  const showSteps = useMe()?.show_tool_steps === true;
  const [turn, setTurn] = useState<ChatMessage[]>([]);
  const [live, setLive] = useState("");
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [decision, setDecision] = useState<DecisionCardData | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const endMarker = useRef<HTMLDivElement>(null);
  const composerBox = useRef<HTMLDivElement>(null);
  const streaming = useRef(false);
  // Read once. The window check covers the server render, which has no URL.
  const [prefill] = useState(() =>
    typeof window === "undefined" ? "" : (readAsk(window.location.search) ?? ""),
  );

  const messages = [...(history.data ?? []), ...turn];

  // Keep the end of the turn in view as it grows. Nothing is sent yet means the
  // person is reading old history, so their place is kept. The margin is the
  // composer's height, so the reply never ends up under the sticky box. The
  // jump is instant, so streamed text does not shake.
  useEffect(() => {
    const marker = endMarker.current;
    if (marker === null || turn.length === 0) {
      return;
    }
    marker.style.scrollMarginBottom = `${composerBox.current?.offsetHeight ?? 0}px`;
    marker.scrollIntoView({ block: "end" });
  }, [turn, live, working, decision, citations, failure]);

  async function send(message: string): Promise<void> {
    // A ref, so a second submit in the same tick sees the first.
    if (streaming.current) {
      return;
    }

    const parsed = chatMessageSchema.safeParse({ message });
    if (!parsed.success) {
      // Checked before any fetch, so a bad message costs no round trip.
      setFailure("VALIDATION");
      return;
    }

    streaming.current = true;
    setWorking(true);
    setFailure(null);
    setSteps([]);
    setCitations([]);
    setDecision(null);
    setLive("");
    setTurn((previous) => [...previous, { role: "user", content: parsed.data.message }]);

    try {
      const response = await postChatMessage(parsed.data);

      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }

      if (!response.ok || response.body === null) {
        // A refusal before the stream starts is a JSON body, not a stream.
        setFailure(await refusalCode(response));
        return;
      }

      await consume(response.body);
    } catch {
      // A dropped socket says nothing about what the turn did.
      setFailure("INTERNAL");
    } finally {
      streaming.current = false;
      setWorking(false);
    }
  }

  async function consume(body: ReadableStream<Uint8Array>): Promise<void> {
    const reader = body.getReader();
    // `stream: true` joins a character split across two reads.
    const decoder = new TextDecoder();
    const parser = createSseParser();
    let text = "";

    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }

      for (const event of parser.push(decoder.decode(value, { stream: true }))) {
        if (event.event === "token") {
          text += readToken(event.data);
          setLive(text);
          continue;
        }
        if (event.event === "step") {
          const step = readStep(event.data);
          if (step !== null) {
            setSteps((previous) => [...previous, step]);
          }
          continue;
        }
        if (event.event === "reply") {
          const reply = readReply(event.data);
          if (reply !== null) {
            // The reply holds the checked text, which may replace the streamed draft.
            text = "";
            setLive("");
            setCitations(reply.citations);
            setDecision(reply.decision);
            setTurn((previous) => [...previous, { role: "assistant", content: reply.reply }]);
          }
          continue;
        }
        if (event.event === "error") {
          // Drop the half-written text: nothing was decided.
          text = "";
          setLive("");
          setDecision(null);
          setFailure(readCode(event.data));
        }
      }
    }
  }

  return (
    <div
      className={`mx-auto flex min-h-[calc(100dvh-var(--shell-offset))] w-full flex-col gap-4 lg:flex-row ${
        showSteps ? "max-w-6xl" : "max-w-3xl"
      }`}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-4">
        <h1 className="text-lg font-semibold">Chat</h1>

        <Conversation messages={messages} live={live} working={working} />

        {failure !== null ? <Alert tone="destructive">{failureMessage(failure)}</Alert> : null}

        {decision !== null ? <DecisionCard card={decision} /> : null}
        <Citations citations={citations} />

        <div ref={endMarker} aria-hidden="true" />

        <div ref={composerBox} className="sticky bottom-0 bg-background pb-4 pt-2">
          <Composer
            onSend={(message) => void send(message)}
            onRefuse={setFailure}
            initialValue={prefill}
          />
        </div>
      </div>

      {showSteps ? (
        <aside
          aria-label="Tool steps column"
          className="w-full lg:w-80 lg:shrink-0 lg:self-end lg:pb-4"
        >
          <ToolSteps steps={steps} />
        </aside>
      ) : null}
    </div>
  );
}

async function refusalCode(response: Response): Promise<string> {
  try {
    return readCode(await response.json());
  } catch {
    return "INTERNAL";
  }
}
