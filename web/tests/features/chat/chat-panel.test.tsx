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
let history: { role: string; content: string }[] = [];
// The past chats the list shows; `history` is what opening any of them returns.
let pastChats: { id: string; title: string; last_seen_at: string }[] = [];
let streamRejects = false;
// jsdom has no `scrollIntoView`; the panel calls it, so this file stubs it.
const scrollStub = vi.fn<(options?: ScrollIntoViewOptions) => void>();

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
  history = [];
  pastChats = [];
  streamRejects = false;
  scrollStub.mockReset();
  Element.prototype.scrollIntoView = scrollStub;
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
    if (url.includes("/api/chat/chats")) {
      return json({ chats: pastChats });
    }
    if (url.includes("/api/chat/messages")) {
      return json({ messages: history });
    }
    if (url.includes("/api/chat/stream")) {
      if (streamRejects) {
        throw new TypeError("network down");
      }
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
  // @ts-expect-error jsdom has no such method; remove the stub so tests share nothing.
  delete Element.prototype.scrollIntoView;
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

describe("ChatPanel page width", () => {
  it.each([false, true])(
    "the chat page fills the shell width (tool steps shown: %s)",
    async (steps) => {
      // Catches a panel that keeps its own width cap or centring: the page would
      // be narrower than the top bar, leaving a gap on the left. The outer box is
      // the parent of the chat list; with steps on, the side column is awaited so
      // the setting has been read before the classes are checked.
      showToolSteps = steps;
      render(
        <QueryProvider>
          <ChatPanel />
        </QueryProvider>,
      );
      await waitFor(() => expect(meCalls).toBeGreaterThan(0));
      if (steps) {
        await screen.findByRole("complementary");
      }

      const outer = screen.getByRole("navigation", { name: "Chats" }).parentElement;
      expect(outer).not.toBeNull();
      expect(outer).not.toHaveClass("max-w-5xl");
      expect(outer).not.toHaveClass("max-w-[88rem]");
      expect(outer).not.toHaveClass("mx-auto");
    },
  );
});

/** Open the chat and send one message, without sending any stream frame. */
async function sendOnly(): Promise<void> {
  render(
    <QueryProvider>
      <ChatPanel />
    </QueryProvider>,
  );
  await waitFor(() => expect(meCalls).toBeGreaterThan(0));
  const user = userEvent.setup();
  await user.type(await screen.findByRole("textbox", { name: "Message" }), "Hello there");
  await user.click(screen.getByRole("button", { name: "Send" }));
}

function replyFrame(decision: unknown): Uint8Array {
  return frame("reply", {
    reply: "Fake answer.",
    outcome: "answered",
    citations: [],
    request_id: null,
    decision,
  });
}

describe("ChatPanel typing dots and follow-the-reply scroll", () => {
  it("shows the dots after a send, before any frame, and clears them on the reply", async () => {
    // Catches a panel that never tracks working (nothing shows while the model
    // thinks), and one that sets it but never clears it on the reply.
    await sendOnly();
    await waitFor(() => expect(streamController).toBeDefined());

    expect(await screen.findByRole("status", { name: "Assistant is working" })).toBeInTheDocument();

    streamController?.enqueue(replyFrame(null));
    streamController?.close();

    await waitFor(() => expect(screen.getByText("Fake answer.")).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });

  it("clears the dots when the turn fails", async () => {
    // Catches a panel that clears working only on the happy path, so a dropped
    // connection leaves the dots pulsing beside the error for ever.
    streamRejects = true;
    await sendOnly();

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("scrolls to the end after the send, after a token and after the reply", async () => {
    // Catches a panel that scrolls on only one of the three growth points: the
    // person's message, streamed text (a live-text-only change), or the final
    // reply. The stub is cleared before each step so an earlier call cannot
    // satisfy a later check.
    await sendOnly();
    await waitFor(() => expect(streamController).toBeDefined());
    await waitFor(() => expect(scrollStub).toHaveBeenCalledWith({ block: "end" }));

    scrollStub.mockClear();
    streamController?.enqueue(frame("token", { text: "Hel" }));
    await screen.findByText("Hel");
    await waitFor(() => expect(scrollStub).toHaveBeenCalledWith({ block: "end" }));

    scrollStub.mockClear();
    streamController?.enqueue(replyFrame(null));
    streamController?.close();
    await screen.findByText("Fake answer.");
    await waitFor(() => expect(scrollStub).toHaveBeenCalledWith({ block: "end" }));
  });

  it("scrolls after the decision card is in the document", async () => {
    // Catches a scroll that runs before the card is drawn (the card then sits
    // below the fold) or one that watches only the reply text and misses the
    // card. The stub records whether the card existed at the moment of each call.
    const seenWithCard: boolean[] = [];
    scrollStub.mockImplementation(() => {
      seenWithCard.push(screen.queryByRole("region", { name: "Decision" }) !== null);
    });
    await sendOnly();
    await waitFor(() => expect(streamController).toBeDefined());
    await waitFor(() => expect(scrollStub).toHaveBeenCalled());

    scrollStub.mockClear();
    seenWithCard.length = 0;
    streamController?.enqueue(
      replyFrame({
        request_id: null,
        status: null,
        decision: "auto",
        risk_class: "low",
        reasons: ["IN_TEMPLATE"],
        approvers: [{ slot: "department_manager", resolved: false }],
        effective_expiry_days: 30,
        expires_at: null,
      }),
    );
    streamController?.close();

    await screen.findByRole("region", { name: "Decision" });
    await waitFor(() => expect(seenWithCard).toContain(true));
    expect(seenWithCard.at(-1)).toBe(true);
  });

  it("makes no scroll call when a past chat is opened and nothing is sent", async () => {
    // Catches a panel that scrolls when a chat's history loads, which would
    // yank a returning person away from where they were reading. The page now
    // opens on a new chat, so the history comes from clicking a chat in the
    // list (its key carries the chat id).
    pastChats = [{ id: "chat-1", title: "Old question", last_seen_at: "2026-06-01T10:00:00Z" }];
    history = [
      { role: "user", content: "Old question" },
      { role: "assistant", content: "Old answer" },
    ];
    render(
      <QueryProvider>
        <ChatPanel />
      </QueryProvider>,
    );

    await userEvent.setup().click(await screen.findByRole("button", { name: "Old question" }));

    expect(await screen.findByText("Old answer")).toBeInTheDocument();
    await waitFor(() => expect(meCalls).toBeGreaterThan(0));
    expect(scrollStub).not.toHaveBeenCalled();
  });
});
