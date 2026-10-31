"use client";

import { useEffect, useRef, useState } from "react";

import { useMe } from "@/features/auth/api";
import {
  postChatMessage,
  useChatHistory,
  useChatList,
  useRefreshAfterReply,
} from "@/features/chat/api";
import { ChatList } from "@/features/chat/components/chat-list";
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
} from "@/features/chat/components/types";
import { failureMessage } from "@/lib/failure-messages";
import { keepOpenChat, readOpenChat } from "@/features/chat/open-chat";
import { readAsk } from "@/features/chat/prefill";
import { chatMessageSchema } from "@/features/chat/schema";
import { readTurnStream, refusalCode } from "@/features/chat/turn-stream";
import { Alert } from "@/components/ui/alert";

/**
 * The chat page. It fills the shell's width, so the chat list in the left
 * column lines up with the top bar. `chatId` is the chat a
 * message goes to (null is a new chat; the first reply names it), and `openedId`
 * is the chat whose history is read, which only a click on the list sets, so a
 * new chat's own lines are never read back on top of themselves. History is a
 * cached query; the live turn is local state, since a stream has no cache key. The decision card, steps and citations belong to
 * the latest turn and clear on a new send or an error. The steps sit in a column
 * beside the chat, level with the composer, and drop under it on a narrow
 * screen. The composer starts with
 * `?ask=` from a renewal link but never sends on its own.
 */
export function ChatPanel() {
  const refreshAfterReply = useRefreshAfterReply();
  const [chatId, setChatId] = useState<string | null>(null);
  const [openedId, setOpenedId] = useState<string | null>(null);
  const history = useChatHistory(openedId);
  const chatList = useChatList();
  // Steps show only when a platform admin turned them on; until `me` answers, they stay hidden.
  const showSteps = useMe()?.show_tool_steps === true;
  const [turn, setTurn] = useState<ChatMessage[]>([]);
  const [live, setLive] = useState("");
  // The newest progress words; shown beside the dots to every user, unlike the steps.
  const [progress, setProgress] = useState("");
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

  const messages = [...(openedId === null ? [] : (history.data ?? [])), ...turn];

  // Coming back from another page reopens the chat that was open, unless a
  // renewal link brought a new question to ask.
  useEffect(() => {
    const kept = readOpenChat();
    if (kept !== null && prefill === "") {
      setChatId(kept);
      setOpenedId(kept);
    }
  }, [prefill]);

  function resetTurn(): void {
    setTurn([]);
    setLive("");
    setProgress("");
    setSteps([]);
    setCitations([]);
    setDecision(null);
    setFailure(null);
  }

  // Not while a reply is streaming: its lines would land in the chat just opened.
  function openChat(id: string): void {
    if (streaming.current) {
      return;
    }
    resetTurn();
    setChatId(id);
    setOpenedId(id);
    keepOpenChat(id);
  }

  function newChat(): void {
    if (streaming.current) {
      return;
    }
    resetTurn();
    setChatId(null);
    setOpenedId(null);
    keepOpenChat(null);
  }

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

    const parsed = chatMessageSchema.safeParse(
      chatId === null ? { message } : { message, chat_id: chatId },
    );
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
    setProgress("");
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

      await readTurnStream(response.body, {
        onLive: setLive,
        onStep: (step) => setSteps((previous) => [...previous, step]),
        onProgress: setProgress,
        onReply: (reply) => {
          setCitations(reply.citations);
          setDecision(reply.decision);
          setTurn((previous) => [...previous, { role: "assistant", content: reply.reply }]);
          if (reply.chatId !== null) {
            setChatId(reply.chatId);
            keepOpenChat(reply.chatId);
          }
          refreshAfterReply();
        },
        onError: (code) => {
          setDecision(null);
          setFailure(code);
        },
      });
    } catch {
      // A dropped socket says nothing about what the turn did.
      setFailure("INTERNAL");
    } finally {
      streaming.current = false;
      setWorking(false);
      setProgress("");
    }
  }

  return (
    <div className="flex min-h-[calc(100dvh-var(--shell-offset))] w-full flex-col gap-4 lg:-mb-6 lg:flex-row">
      <ChatList
        chats={chatList.data}
        activeId={chatId}
        onOpen={openChat}
        onNew={newChat}
        failed={chatList.isError}
      />
      <div className="flex min-w-0 flex-1 flex-col gap-4">
        <h1 className="text-lg font-semibold">Chat</h1>

        <Conversation
          messages={messages}
          live={live}
          working={working}
          progress={progress}
        />

        {failure !== null ? <Alert tone="destructive">{failureMessage(failure)}</Alert> : null}

        {decision !== null ? <DecisionCard card={decision} /> : null}
        <Citations citations={citations} />

        <div ref={endMarker} aria-hidden="true" />

        {/* The free space sits below the sources, so they stay under their reply. */}
        <div aria-hidden="true" className="flex-1" />

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
