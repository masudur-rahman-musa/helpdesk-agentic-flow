/**
 * The Server-Sent Events frame parser, and nothing else.
 *
 * Hand written rather than a dependency: a browser `EventSource` cannot send a
 * POST or an `Authorization` header (19c decision D1), so the turn is read off
 * a `fetch` body either way and the library's main value does not apply.
 *
 * Pure — strings in, events out. No fetch, no React, no `TextDecoder`: the
 * reader loop decodes, because it is the only thing that can join a multi-byte
 * character split across two network reads, and the parser stays testable with
 * plain strings.
 *
 * **A frame ends at a blank line and its `data` is ONE line.** The buffer is
 * split on the raw frame boundary BEFORE anything is JSON-decoded, which is
 * what makes `agent_api.sse.encode_event`'s escaping load-bearing: a reply
 * containing "\n\nevent: reply" cannot forge a second event here. Pinned by
 * `tests/sse-parser.test.ts`'s "keeps a payload carrying an escaped newline as
 * ONE event".
 *
 * Three things a malformed stream may not do: end the read loop, emit half a
 * frame, or dispatch an event the server does not send. Each is pinned by its
 * own case in `tests/sse-parser.test.ts` ("skips a data line that is not JSON
 * and still parses the next frame", "holds a trailing partial frame instead of
 * emitting it", "skips an event name outside the five the server sends").
 */

/** Exactly agent-api's five names (`agent_api/sse.py`). Anything else is dropped. */
export const SSE_EVENT_NAMES = ["step", "token", "reply", "error", "done"] as const;

export type SseEventName = (typeof SSE_EVENT_NAMES)[number];

/** One frame, with its payload already decoded. */
export type SseEvent = { event: SseEventName; data: unknown };

export type SseParser = {
  /** The events completed by this chunk, in arrival order. */
  push(chunk: string): SseEvent[];
};

const FRAME_SEPARATOR = "\n\n";

function isEventName(name: string): name is SseEventName {
  return (SSE_EVENT_NAMES as readonly string[]).includes(name);
}

/**
 * Reads one raw frame. Returns `null` for every frame the client cannot act
 * on — an unknown name, a missing field, a `data` line that is not JSON — so
 * one bad frame costs its own event and nothing more.
 */
function parseFrame(raw: string): SseEvent | null {
  let name: string | null = null;
  let payload: string | null = null;

  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) {
      name = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      payload = line.slice("data:".length).trim();
    }
  }

  if (name === null || payload === null || !isEventName(name)) {
    return null;
  }

  try {
    return { event: name, data: JSON.parse(payload) };
  } catch {
    return null;
  }
}

/**
 * One parser per stream. The cross-read buffer is per turn on purpose: a
 * shared one would carry half a frame from an abandoned turn into the next.
 */
export function createSseParser(): SseParser {
  let buffer = "";

  return {
    push(chunk: string): SseEvent[] {
      buffer += chunk;
      const parts = buffer.split(FRAME_SEPARATOR);
      // The last part is whatever arrived after the final boundary: a complete
      // chunk leaves "" here, a truncated one leaves the partial frame.
      buffer = parts.pop() ?? "";

      const events: SseEvent[] = [];
      for (const raw of parts) {
        if (raw.trim().length === 0) {
          continue;
        }
        const event = parseFrame(raw);
        if (event !== null) {
          events.push(event);
        }
      }
      return events;
    },
  };
}
