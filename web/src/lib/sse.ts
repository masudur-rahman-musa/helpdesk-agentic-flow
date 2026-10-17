/**
 * The Server-Sent Events frame parser. Hand written: `EventSource` cannot POST
 * or send an `Authorization` header, so the turn is read off a `fetch` body.
 * Frames are split before JSON decoding, so a reply holding "\n\nevent: reply"
 * cannot fake a second event. A bad frame is dropped and never ends the loop.
 */

/** The five event names agent-api sends. Anything else is dropped. */
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
 * One raw frame, or `null` for anything the client cannot act on, so one bad
 * frame costs only itself.
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
