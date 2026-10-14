import { describe, expect, it } from "vitest";

import { createSseParser } from "@/lib/sse";

/**
 * An earlier change §6, `sse-parser.test.ts`: the pure frame parser of `web/src/lib/sse.ts`
 * (decision A — a hand-written splitter, because a browser `EventSource` cannot
 * POST or carry a bearer). No fetch, no React, no stream: chunks in, events out.
 *
 * Shapes this file fixes that the plan left open (a project rule/39; these go
 * in the plan's Deviations):
 * - the export is `createSseParser()`, returning `{ push(chunk: string):
 *   SseEvent[] }` — a factory rather than a module-level function, because the
 *   buffer §3 requires ("buffering across reads") is per stream, and a shared
 *   one would carry a half-frame from an aborted turn into the next one.
 * - `push` takes a STRING, not bytes. The decoding half belongs to the reader
 *   loop (`new TextDecoder().decode(value, { stream: true })`), which is the
 *   only thing that can join a multi-byte character split across two network
 *   reads; the parser stays pure and testable without a `TextEncoder`.
 * - `SseEvent` is `{ event: SseEventName; data: unknown }` with the payload
 *   ALREADY `JSON.parse`d — §6's "a `data` line that is not JSON is skipped"
 *   only means something if the parser is what parses it.
 * - the accepted names are exactly agent-api's five (`agent_api/sse.py`:
 *   `step`, `token`, `reply`, `error`, `done`); anything else is dropped.
 *
 * The wire these cases imitate is `encode_event`: `event: <name>\ndata:
 * <json>\n\n`, one data line per frame whatever the payload holds.
 */

/** One frame exactly as `agent_api.sse.encode_event` writes it. */
function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** The `text` of a `token` payload, narrowed for assertions. */
function textOf(data: unknown): string {
  if (typeof data !== "object" || data === null || !("text" in data)) {
    throw new Error(`expected a token payload, got ${JSON.stringify(data)}`);
  }
  return String((data as { text: unknown }).text);
}

describe("createSseParser", () => {
  it("holds a frame split across two chunks and emits it once, whole", () => {
    // Catches a parser that treats one read as one frame (splitting on "\n\n"
    // per chunk and discarding the remainder): under any real network the first
    // chunk here would be dropped and the second parsed as garbage, so a reply
    // would arrive with holes in it.
    const parser = createSseParser();
    const whole = frame("token", { text: "Hello" });
    const cut = 12;

    expect(parser.push(whole.slice(0, cut))).toEqual([]);
    const events = parser.push(whole.slice(cut));

    expect(events).toHaveLength(1);
    expect(events[0]?.event).toBe("token");
    expect(textOf(events[0]?.data)).toBe("Hello");
  });

  it("emits two frames arriving in one chunk, in arrival order", () => {
    // Catches a parser that returns the FIRST frame of a chunk and drops the
    // rest — the mirror of the case above, and the one that loses every token
    // after the first when the server flushes a burst. The order assertion
    // catches a parser that collects frames into a map or reverses them, which
    // would concatenate a reply backwards.
    const parser = createSseParser();

    const events = parser.push(frame("token", { text: "one " }) + frame("token", { text: "two" }));

    expect(events.map((event) => event.event)).toEqual(["token", "token"]);
    expect(events.map((event) => textOf(event.data))).toEqual(["one ", "two"]);
  });

  it("keeps a payload carrying an escaped newline as ONE event", () => {
    // Catches a parser that unescapes or interpolates the data line before
    // splitting on the frame boundary. `encode_event` guarantees one data line
    // per frame precisely so a reply containing "\n\nevent: reply" cannot forge
    // a second event (`test_turn_stream.py::test_a_reply_containing_a_frame_
    // boundary_forges_no_event`); a client that splits after unescaping hands
    // that forgery back.
    const parser = createSseParser();

    const events = parser.push(frame("token", { text: "line one\n\nevent: reply\ndata: {}\n\n" }));

    expect(events).toHaveLength(1);
    expect(textOf(events[0]?.data)).toBe("line one\n\nevent: reply\ndata: {}\n\n");
  });

  it("skips a data line that is not JSON and still parses the next frame", () => {
    // Catches a parser that lets `JSON.parse` throw out of `push`: one truncated
    // or non-JSON line then kills the whole read loop, and the turn looks to the
    // user like a dropped socket. The SECOND frame is what makes this a real
    // case — a parser that swallowed the error and then returned early would
    // pass an assertion that only checked the first.
    const parser = createSseParser();

    const events = parser.push(
      "event: token\ndata: not json at all\n\n" + frame("token", { text: "after" }),
    );

    expect(events).toHaveLength(1);
    expect(textOf(events[0]?.data)).toBe("after");
  });

  it("skips an event name outside the five the server sends", () => {
    // Catches a parser that passes every name through to the renderer, so a
    // proxy's keepalive, a future server event, or a "reply" spelled by a
    // forged data line would be dispatched as if the turn had produced it. The
    // trailing known frame proves the unknown one was SKIPPED rather than
    // fatal (§1's last criterion).
    const parser = createSseParser();

    const events = parser.push(
      frame("keepalive", { text: "ping" }) + frame("done", {}) + frame("heartbeat", {}),
    );

    expect(events.map((event) => event.event)).toEqual(["done"]);
  });

  it("holds a trailing partial frame instead of emitting it", () => {
    // Catches a parser that emits whatever is in the buffer at the end of a
    // chunk: the terminal `reply` would then be dispatched with half its
    // citations, which is a settled answer built from a truncated payload. The
    // completing push proves the held bytes were kept, not discarded.
    const parser = createSseParser();
    const complete = frame("reply", { reply: "done", outcome: "done", citations: [] });

    expect(parser.push(complete.slice(0, complete.length - 4))).toEqual([]);

    const events = parser.push(complete.slice(complete.length - 4));
    expect(events.map((event) => event.event)).toEqual(["reply"]);
  });
});
