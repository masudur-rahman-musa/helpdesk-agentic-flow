import {
  type ReplyEvent,
  type StepEvent,
  readCode,
  readProgress,
  readReply,
  readStep,
  readToken,
} from "@/features/chat/components/types";
import { createSseParser } from "@/lib/sse";

/** What the panel does with each part of a streamed turn. */
export type TurnHandlers = {
  onLive: (text: string) => void;
  onStep: (step: StepEvent) => void;
  onProgress: (words: string) => void;
  onReply: (reply: ReplyEvent) => void;
  onError: (code: string) => void;
};

/**
 * Reads one turn's stream to its end. The text so far is handed over whole
 * through `onLive`, and cleared (`""`) before a reply or an error, so a
 * half-written draft never outlives the checked text or a failure.
 */
export async function readTurnStream(
  body: ReadableStream<Uint8Array>,
  handlers: TurnHandlers,
): Promise<void> {
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
        handlers.onLive(text);
        continue;
      }
      if (event.event === "progress") {
        const words = readProgress(event.data);
        if (words !== "") {
          handlers.onProgress(words);
        }
        continue;
      }
      if (event.event === "step") {
        const step = readStep(event.data);
        if (step !== null) {
          handlers.onStep(step);
        }
        continue;
      }
      if (event.event === "reply") {
        const reply = readReply(event.data);
        if (reply !== null) {
          // The reply holds the checked text, which may replace the streamed draft.
          text = "";
          handlers.onLive("");
          handlers.onReply(reply);
        }
        continue;
      }
      if (event.event === "error") {
        // Drop the half-written text: nothing was decided.
        text = "";
        handlers.onLive("");
        handlers.onError(readCode(event.data));
      }
    }
  }
}

/** The typed code of a refusal that came before the stream, or `INTERNAL`. */
export async function refusalCode(response: Response): Promise<string> {
  try {
    return readCode(await response.json());
  } catch {
    return "INTERNAL";
  }
}
