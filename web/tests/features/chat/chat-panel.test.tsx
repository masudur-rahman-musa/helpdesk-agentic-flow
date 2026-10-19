import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { ChatPanel } from "@/features/chat/components/chat-panel";

/**
 * `chat-panel.test.tsx`: the tool steps are a platform admin's choice, read
 * from `/api/me` as `show_tool_steps`. Off means no steps box at all; on means
 * the steps sit in a column beside the chat. jsdom, a stubbed `fetch`.
 *
 * Shapes this file fixes:
 * - the steps column is an `aside` (role `complementary`), next to the column
 *   that holds the conversation and the composer, inside one shared parent.
 * - that shared parent turns into a row from a breakpoint up (a `sm:`, `md:`,
 *   `lg:` or `xl:` `flex-row` class), so a narrow screen drops the column
 *   under the chat. jsdom has no layout, so the class is all it can show.
 */

const fetchStub = vi.fn<typeof fetch>();
let showToolSteps = false;
let meCalls = 0;
let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function frame(event: string, data: unknown): Uint8Array {
  return new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  return input instanceof URL ? input.toString() : input.url;
}

beforeEach(() => {
  meCalls = 0;
  streamController = undefined;
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes("/api/me")) {
      meCalls += 1;
      return json({
        display_name: "Fake Person One",
        principals: ["employee"],
        tabs: ["chat"],
        show_tool_steps: showToolSteps,
      });
    }
    if (url.includes("/api/chat/messages")) {
      return json({ messages: [] });
    }
    if (url.includes("/api/chat/stream")) {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller;
        },
      });
      return new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function sendAndStream(): Promise<void> {
  render(
    <QueryProvider>
      <ChatPanel />
    </QueryProvider>,
  );
  await waitFor(() => expect(meCalls).toBeGreaterThan(0));
  const user = userEvent.setup();
  await user.type(await screen.findByRole("textbox", { name: "Message" }), "Hello there");
  await user.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(streamController).toBeDefined());
  streamController?.enqueue(
    frame("step", {
      node: "retrieve",
      round: 1,
      tools: [{ tool: "search_policy", ok: true, error_code: null }],
    }),
  );
}

describe("ChatPanel tool steps and the show_tool_steps setting", () => {
  it("renders no tool steps when show_tool_steps is false, even after a step event", async () => {
    // Catches a panel that ignores the setting and always renders the steps
    // box, or hides only the list and leaves the "Tool steps" heading behind.
    // The step event is followed by the reply, and the reply is awaited, so the
    // step has certainly been read by the time the absence is checked.
    showToolSteps = false;
    await sendAndStream();
    streamController?.enqueue(
      frame("reply", {
        reply: "Fake answer.",
        outcome: "answered",
        citations: [],
        request_id: null,
        decision: null,
      }),
    );
    streamController?.close();

    const log = await screen.findByRole("log", { name: "Conversation" });
    await waitFor(() => expect(within(log).getByText("Fake answer.")).toBeInTheDocument());

    expect(screen.queryByRole("list", { name: "Tool steps" })).toBeNull();
    expect(screen.queryByText("Tool steps")).toBeNull();
    expect(screen.queryByText("search_policy")).toBeNull();
  });

  it("renders the steps in a side column next to the chat when true", async () => {
    // Catches steps left in the old block under the messages, and a column that
    // is not beside the chat: the aside must share a parent with the column
    // that holds the composer, hold the steps and not the composer, and the
    // parent must become a row from a breakpoint up so a narrow screen stacks.
    showToolSteps = true;
    await sendAndStream();

    const timeline = await screen.findByRole("list", { name: "Tool steps" });
    const aside = screen.getByRole("complementary");
    const composer = screen.getByRole("textbox", { name: "Message" });

    expect(aside).toContainElement(timeline);
    expect(aside).not.toContainElement(composer);
    const parent = aside.parentElement;
    expect(parent).not.toBeNull();
    const chatColumn = Array.from(parent?.children ?? []).find((child) => child.contains(composer));
    expect(chatColumn).toBeDefined();
    expect(chatColumn).not.toBe(aside);
    expect(chatColumn).toContainElement(screen.getByRole("log", { name: "Conversation" }));
    expect(parent?.className).toMatch(/(sm|md|lg|xl):flex-row/);
  });
});
