"use client";

import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { Citations } from "@/components/chat/citations";
import { Composer } from "@/components/chat/composer";
import { Conversation } from "@/components/chat/conversation";
import { DecisionCard } from "@/components/chat/decision-card";
import { ToolSteps } from "@/components/chat/tool-steps";
import {
  type ChatMessage,
  type Citation,
  type DecisionCard as DecisionCardData,
  type StepEvent,
  readCode,
  readHistory,
  readReply,
  readStep,
  readToken,
} from "@/components/chat/types";
import { failureMessage } from "@/lib/chat-errors";
import { chatMessageSchema } from "@/lib/chat-schema";
import { createSseParser } from "@/lib/sse";

/**
 * The chat surface: history, the streamed turn, its tool steps, its decision
 * card, its citations and its failures.
 *
 * Two sources, deliberately different (decision D). The history is a TanStack
 * Query read of `GET /api/chat/messages` — a cached read with a key worth
 * invalidating — and the live turn is local state, because a stream has no
 * cache key and settling it into the query cache would mean writing a reply
 * the server has not been asked for again.
 *
 * Both calls go to this app's own routes, never to agent-api: the bearer is an
 * httpOnly cookie the browser cannot read (22a decision A), so a cross-origin
 * call would have nothing to send. An expired session comes back from
 * `proxyAgentApi` as a redirect the browser already followed, which is why
 * `response.redirected` is what this component checks rather than a status —
 * decision E, so the expiry path stays in one place.
 *
 * The decision card, the tool steps and the citations share one lifetime
 * (22d decision D): all three belong to the LATEST turn, are cleared when a
 * new one is sent, and are cleared again by an `error` frame — a decision left
 * standing beside "nothing was changed" is the same class of lie as the
 * half-composed token text the error path already drops. `GET
 * /api/chat/messages` carries no decision at all, which is why a card per
 * assistant message was not an option: every message the history read supplies
 * would have a blank one.
 *
 * The turn's own failures are typed codes, never sentences: `FAILURE_MESSAGES`
 * turns a code into the one sentence the app shows for it, and the alert
 * region holds exactly one at a time.
 */
export function ChatPanel() {
  const history = useQuery({ queryKey: ["chat", "messages"], queryFn: fetchHistory });
  const [turn, setTurn] = useState<ChatMessage[]>([]);
  const [live, setLive] = useState("");
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [decision, setDecision] = useState<DecisionCardData | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const streaming = useRef(false);

  const messages = [...(history.data ?? []), ...turn];

  async function send(message: string): Promise<void> {
    // The composer stays enabled through a turn (§1's unhappy paths), so the
    // guard against two overlapping streams lives here instead of on the
    // button. A ref and not state: a second submit in the same tick must see
    // the first one's decision.
    if (streaming.current) {
      return;
    }

    const parsed = chatMessageSchema.safeParse({ message });
    if (!parsed.success) {
      // The same pre-check the route handler runs, before any fetch: an
      // unsendable message spends neither a round trip nor a rate-limit slot.
      setFailure("VALIDATION");
      return;
    }

    streaming.current = true;
    setFailure(null);
    setSteps([]);
    setCitations([]);
    setDecision(null);
    setLive("");
    setTurn((previous) => [...previous, { role: "user", content: parsed.data.message }]);

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(parsed.data),
      });

      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }

      if (!response.ok || response.body === null) {
        // A refusal before the first byte — 429 `POLICY_BLOCKED` from the rate
        // limit, 400 `VALIDATION` from the cap — is a JSON envelope, not a
        // stream. Feeding it to the parser would show a turn that started and
        // silently produced nothing.
        setFailure(await refusalCode(response));
        return;
      }

      await consume(response.body);
    } catch {
      // A dropped socket and an unreachable proxy are the same thing to a
      // reader: neither says anything about what the turn did, which is what
      // `INTERNAL` means.
      setFailure("INTERNAL");
    } finally {
      streaming.current = false;
    }
  }

  /** Reads the stream to its end, applying each frame as it arrives. */
  async function consume(body: ReadableStream<Uint8Array>): Promise<void> {
    const reader = body.getReader();
    // `stream: true` is what joins a multi-byte character split across two
    // reads; the parser never sees bytes.
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
            // The tokens are dropped rather than kept: the reply event carries
            // the CHECKED text, and a regenerated turn streams a composition
            // the post-check replaced.
            text = "";
            setLive("");
            setCitations(reply.citations);
            setDecision(reply.decision);
            setTurn((previous) => [...previous, { role: "assistant", content: reply.reply }]);
          }
          continue;
        }
        if (event.event === "error") {
          // A half-composed sentence left standing would be prose about a turn
          // nothing decided, which is why `_failure_frames` sends no `reply`
          // with an error either.
          text = "";
          setLive("");
          setDecision(null);
          setFailure(readCode(event.data));
        }
        // `done` needs nothing: the socket closing is what ends this loop, and
        // the frame exists so a client can tell a finished turn from a dropped
        // one.
      }
    }
  }

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <h1 className="text-lg font-semibold">Chat</h1>

      <Conversation messages={messages} live={live} />

      {failure !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {failureMessage(failure)}
        </p>
      ) : null}

      {decision !== null ? <DecisionCard card={decision} /> : null}
      <ToolSteps steps={steps} />
      <Citations citations={citations} />

      <Composer onSend={(message) => void send(message)} onRefuse={setFailure} />
    </div>
  );
}

/** The typed code of a refusal body, or the default when it carries none. */
async function refusalCode(response: Response): Promise<string> {
  try {
    return readCode(await response.json());
  } catch {
    return "INTERNAL";
  }
}

async function fetchHistory(): Promise<ChatMessage[]> {
  const response = await fetch("/api/chat/messages", { headers: { accept: "application/json" } });

  if (response.redirected) {
    window.location.assign(response.url);
    return [];
  }
  if (!response.ok) {
    // Thrown, not swallowed: an empty conversation and an unreadable one are
    // different things, and Query is what decides whether to retry.
    throw new Error(`the transcript could not be read (${response.status})`);
  }
  return readHistory(await response.json());
}
