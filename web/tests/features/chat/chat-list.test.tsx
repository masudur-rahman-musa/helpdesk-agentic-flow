import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryProvider } from "@/components/layout/query-provider";
import { ChatList } from "@/features/chat/components/chat-list";
import { ChatPanel } from "@/features/chat/components/chat-panel";

/**
 * `chat-list.test.tsx`: the left column of past chats and what the panel does
 * with it. jsdom, a stubbed `fetch`, no browser, no agent-api.
 *
 * Shapes this file fixes:
 * - `ChatList` takes `{ chats, activeId, onOpen, onNew, failed }`. `chats` is
 *   `undefined` while loading. It shows one `button` per chat whose accessible
 *   name is the title, in the order given, and one `button` named "New chat".
 *   The active chat's button has `aria-current="true"`. `failed` shows an alert.
 * - `ChatPanel` still takes no props and opens on an empty new chat. It reads
 *   the list from `GET /api/chat/chats` (`{ chats: [{ id, title, last_seen_at }] }`),
 *   one chat's history from `GET /api/chat/messages?chat_id=<id>`, and the id
 *   of a new chat from `chat_id` in the `reply` frame. The stream body carries
 *   `chat_id` only for a chat that already exists.
 */

const fetchStub = vi.fn<typeof fetch>();
const scrollStub = vi.fn<(options?: ScrollIntoViewOptions) => void>();

type Chat = { id: string; title: string; last_seen_at: string };
type Line = { role: string; content: string };

let chats: Chat[] = [];
let chatsStatus = 200;
let chatListCalls = 0;
let histories: Record<string, Line[]> = {};
let historyRequests: string[] = [];
let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;

const OLD: Chat = { id: "chat-old", title: "Alpha old chat", last_seen_at: "2026-06-01T10:00:00Z" };
const NEW: Chat = { id: "chat-new", title: "Zeta new chat", last_seen_at: "2026-06-02T10:00:00Z" };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function frame(event: string, data: unknown): Uint8Array {
  return new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

function replyFrame(chatId: string): Uint8Array {
  return frame("reply", {
    reply: "Fake answer.",
    outcome: "answered",
    citations: [],
    request_id: null,
    decision: null,
    chat_id: chatId,
  });
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  return input instanceof URL ? input.toString() : input.url;
}

function streamBodies(): Record<string, unknown>[] {
  return fetchStub.mock.calls
    .filter(([input]) => urlOf(input).includes("/api/chat/stream"))
    .map(([, init]) => JSON.parse(String(init?.body)) as Record<string, unknown>);
}

beforeEach(() => {
  chats = [];
  chatsStatus = 200;
  chatListCalls = 0;
  histories = {};
  historyRequests = [];
  streamController = undefined;
  scrollStub.mockReset();
  Element.prototype.scrollIntoView = scrollStub;
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    // The "My requests" panel beside the chat reads these two; empty lists.
    if (url.includes("/api/me/requests")) {
      return new Response(JSON.stringify({ requests: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/api/me/tickets")) {
      return new Response(JSON.stringify({ tickets: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/api/me")) {
      return json({
        display_name: "Fake Person One",
        principals: ["employee"],
        tabs: ["chat"],
        show_tool_steps: false,
      });
    }
    if (url.includes("/api/chat/chats")) {
      chatListCalls += 1;
      return chatsStatus === 200 ? json({ chats }) : json({ code: "INTERNAL" }, chatsStatus);
    }
    if (url.includes("/api/chat/messages")) {
      historyRequests.push(url);
      const id = new URL(url, "http://localhost").searchParams.get("chat_id") ?? "";
      return json({ messages: histories[id] ?? [] });
    }
    if (url.includes("/api/chat/stream")) {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller;
        },
      });
      return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
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

function renderPanel() {
  return render(
    <QueryProvider>
      <ChatPanel />
    </QueryProvider>,
  );
}

async function send(text: string): Promise<void> {
  const user = userEvent.setup();
  await user.type(await screen.findByRole("textbox", { name: "Message" }), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
}

describe("ChatList", () => {
  it("the chat list sits on its own muted background", () => {
    // Catches a column left on the page background (or given a hard new colour):
    // it must read as its own part, close to the page, from the theme's muted
    // token at half strength, with rounded corners.
    render(
      <ChatList
        chats={[]}
        activeId={null}
        onOpen={() => undefined}
        onNew={() => undefined}
        failed={false}
      />,
    );

    const nav = screen.getByRole("navigation", { name: "Chats" });
    expect(nav).toHaveClass("bg-muted/50");
    expect(nav).toHaveClass("rounded-md");
    expect(nav).not.toHaveClass("bg-background");
  });

  it("shows the titles in the order the server gave, not sorted again", () => {
    // Catches a list that re-sorts (by title or by id): the order given is
    // "Zeta" before "Alpha", the reverse of alphabetical, so only a list that
    // keeps the server's newest-first order passes.
    render(
      <ChatList
        chats={[
          { id: "z", title: "Zeta newest", lastSeenAt: "2026-06-02T10:00:00Z" },
          { id: "a", title: "Alpha oldest", lastSeenAt: "2026-06-01T10:00:00Z" },
        ]}
        activeId={null}
        onOpen={() => undefined}
        onNew={() => undefined}
        failed={false}
      />,
    );

    const first = screen.getByRole("button", { name: "Zeta newest" });
    const second = screen.getByRole("button", { name: "Alpha oldest" });
    expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("marks only the open chat and calls back with the id or for a new chat", async () => {
    // Catches a list that ignores `activeId`, marks every row, passes the title
    // instead of the id to `onOpen`, or wires "New chat" to `onOpen`.
    const onOpen = vi.fn<(id: string) => void>();
    const onNew = vi.fn<() => void>();
    render(
      <ChatList
        chats={[
          { id: "z", title: "Zeta newest", lastSeenAt: "2026-06-02T10:00:00Z" },
          { id: "a", title: "Alpha oldest", lastSeenAt: "2026-06-01T10:00:00Z" },
        ]}
        activeId="a"
        onOpen={onOpen}
        onNew={onNew}
        failed={false}
      />,
    );
    const user = userEvent.setup();

    expect(screen.getByRole("button", { name: "Alpha oldest" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(screen.getByRole("button", { name: "Zeta newest" })).not.toHaveAttribute(
      "aria-current",
      "true",
    );

    await user.click(screen.getByRole("button", { name: "Zeta newest" }));
    expect(onOpen).toHaveBeenCalledExactlyOnceWith("z");
    expect(onNew).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(onNew).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("still offers New chat while loading and when the list failed", () => {
    // Catches a list that hides the button until the list has loaded, which
    // would leave a person with a failed list no way to start a chat.
    const { rerender } = render(
      <ChatList
        chats={undefined}
        activeId={null}
        onOpen={vi.fn()}
        onNew={vi.fn()}
        failed={false}
      />,
    );
    expect(screen.getByRole("button", { name: "New chat" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();

    rerender(
      <ChatList chats={undefined} activeId={null} onOpen={vi.fn()} onNew={vi.fn()} failed={true} />,
    );
    expect(screen.getByRole("button", { name: "New chat" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});

describe("ChatPanel with the chat list", () => {
  it("shows the titles the server sent, newest first", async () => {
    // Catches a panel that does not read `/api/chat/chats`, or reverses what it
    // read: the list is fed [NEW, OLD] and must show it in that order.
    chats = [NEW, OLD];
    renderPanel();

    const newest = await screen.findByRole("button", { name: NEW.title });
    const oldest = screen.getByRole("button", { name: OLD.title });
    expect(newest.compareDocumentPosition(oldest) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("opens the clicked chat: asks for its history by id and shows it", async () => {
    // Catches a click that loads the wrong chat or none, and a history read
    // that leaves `chat_id` off the request (the server would answer for no
    // chat). Two chats hold different lines, so a mix-up shows.
    chats = [NEW, OLD];
    histories = {
      [OLD.id]: [
        { role: "user", content: "old question" },
        { role: "assistant", content: "old answer" },
      ],
      [NEW.id]: [{ role: "user", content: "new question" }],
    };
    renderPanel();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: OLD.title }));

    const log = screen.getByRole("log", { name: "Conversation" });
    expect(await within(log).findByText("old answer")).toBeInTheDocument();
    expect(within(log).queryByText("new question")).toBeNull();
    const asked = historyRequests.map((url) =>
      new URL(url, "http://localhost").searchParams.get("chat_id"),
    );
    expect(asked).toContain(OLD.id);
    expect(asked).not.toContain(NEW.id);
    expect(screen.getByRole("button", { name: OLD.title })).toHaveAttribute("aria-current", "true");
    // Opening a chat to read must not move the page.
    expect(scrollStub).not.toHaveBeenCalled();
  });

  it("sends the chat id when a message is sent in an opened chat", async () => {
    // Catches a panel that always starts a new chat: a send inside an opened
    // chat has to name it, or the reply lands in a chat nobody is looking at.
    chats = [OLD];
    histories = { [OLD.id]: [{ role: "user", content: "old question" }] };
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: OLD.title }));
    await screen.findByText("old question");

    await send("follow up");

    await waitFor(() => expect(streamBodies()).toHaveLength(1));
    expect(streamBodies()[0]).toEqual({ message: "follow up", chat_id: OLD.id });
  });

  it("clears the conversation on New chat and sends no chat id", async () => {
    // Catches a New chat button that keeps the old lines on screen, or one that
    // keeps sending the old chat's id: the next message would be filed in the
    // chat the person just left.
    chats = [OLD];
    histories = {
      [OLD.id]: [
        { role: "user", content: "old question" },
        { role: "assistant", content: "old answer" },
      ],
    };
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: OLD.title }));
    await screen.findByText("old answer");

    await user.click(screen.getByRole("button", { name: "New chat" }));

    await waitFor(() => expect(screen.queryByText("old answer")).toBeNull());
    expect(screen.queryByText("old question")).toBeNull();
    expect(screen.getByRole("button", { name: OLD.title })).not.toHaveAttribute(
      "aria-current",
      "true",
    );

    await send("a fresh start");
    await waitFor(() => expect(streamBodies()).toHaveLength(1));
    const body = streamBodies()[0];
    expect(body).toEqual({ message: "a fresh start" });
    expect(body).not.toHaveProperty("chat_id");
  });

  it("opens on a new chat and sends no chat id for the first message", async () => {
    // Catches a panel that opens the newest chat by itself, or sends an empty
    // `chat_id` string for a new chat (the server must see the key absent).
    chats = [OLD];
    histories = { [OLD.id]: [{ role: "user", content: "old question" }] };
    renderPanel();
    await screen.findByRole("button", { name: OLD.title });
    expect(screen.queryByText("old question")).toBeNull();
    expect(historyRequests).toEqual([]);

    await send("hello");

    await waitFor(() => expect(streamBodies()).toHaveLength(1));
    expect(streamBodies()[0]).toEqual({ message: "hello" });
  });

  it("adds the new chat to the list after the first reply and keeps using its id", async () => {
    // Catches a panel that never refreshes the list after a first reply (the
    // new chat stays missing until a reload), one that does not read `chat_id`
    // off the reply (the second message would start another chat), and one
    // that refreshes before the reply arrives.
    chats = [OLD];
    renderPanel();
    await screen.findByRole("button", { name: OLD.title });
    const callsBefore = chatListCalls;
    expect(screen.queryByRole("button", { name: NEW.title })).toBeNull();

    await send("first message");
    await waitFor(() => expect(streamController).toBeDefined());
    expect(chatListCalls).toBe(callsBefore);

    chats = [NEW, OLD];
    streamController?.enqueue(replyFrame(NEW.id));
    streamController?.close();

    expect(await screen.findByRole("button", { name: NEW.title })).toBeInTheDocument();
    expect(chatListCalls).toBeGreaterThan(callsBefore);
    expect(screen.getByRole("button", { name: NEW.title })).toHaveAttribute("aria-current", "true");

    streamController = undefined;
    await send("second message");
    await waitFor(() => expect(streamBodies()).toHaveLength(2));
    expect(streamBodies()[1]).toEqual({ message: "second message", chat_id: NEW.id });
  });

  it("shows an alert when the list fails, and the chat still works", async () => {
    // Catches a panel that lets a failed list take the chat down (a thrown
    // render, a disabled composer), or fails silently with no alert. The query
    // client does not retry here, so the alert is not waiting on a retry timer.
    chatsStatus = 500;
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <ChatPanel />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New chat" })).toBeInTheDocument();

    await send("hello anyway");
    await waitFor(() => expect(streamController).toBeDefined());
    streamController?.enqueue(replyFrame("chat-from-reply"));
    streamController?.close();

    expect(await screen.findByText("Fake answer.")).toBeInTheDocument();
    expect(streamBodies()[0]).toEqual({ message: "hello anyway" });
  });
});
