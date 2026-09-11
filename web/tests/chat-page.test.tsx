import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPanel } from "@/components/chat/chat-panel";
import { QueryProvider } from "@/components/query-provider";
import { FAILURE_MESSAGES } from "@/lib/chat-errors";

/**
 * An earlier change §6, `chat-page.test.tsx`: the chat surface against a scripted stream
 * and a stubbed `fetch`. jsdom, no browser, no agent-api.
 *
 * Shapes this file fixes that the plan left open (a project rule/39; these go
 * in the plan's Deviations):
 * - the unit under test is `ChatPanel` from `web/components/chat/chat-panel.tsx`
 *   — the client component `app/chat/page.tsx` renders inside the shell —
 *   taking **no props**. Everything it shows comes from the two route handlers
 *   (`GET /api/chat/messages` for history, `POST /api/chat/stream` for the
 *   turn), which is what makes the page a thin server wrapper; a props-driven
 *   panel would move the fetching into a component no test here drives. The
 *   page itself is not rendered directly because a server component may be
 *   `async`, and `render()` cannot await one.
 * - it is rendered inside 22a's `QueryProvider`, because the history read is a
 *   TanStack Query read (decision D).
 * - each message in the conversation is an element with `role="article"`
 *   carrying `data-role="user" | "assistant"`. §6 fixed the log's role and name
 *   but not how the two authors are told apart, and §1 requires exactly that.
 * - a citation renders its `score` as the raw wire number (`0.42`), not as a
 *   percentage or a star rating.
 * - the tool-step timeline renders exactly ONE `listitem` per `step` event;
 *   the per-tool entries inside a row are not themselves list items, so the
 *   row count is the event count.
 *
 * The wire shapes are agent-api's as BUILT, not as remembered:
 * `step` → `{node, round, tools:[{tool, ok, error_code}]}` (`graph/build.py::_step_data`),
 * `reply` → `{reply, outcome, citations, request_id, decision}` (`::_reply_data`);
 * `decision` is the turn's decision card or null, which nothing
 * here reads until an earlier change renders it,
 * `error` → `{code}` (`::_failure_frames`), citations → `{document, title,
 * version, heading_path, chunk_id, text, score}` (`graph/state.py::Citation`),
 * history → `{messages:[{role, content}]}` (`chat_routes.ChatHistoryResponse`).
 */

const CITATION_TITLE = "Fake Access Policy";
const CITATION_TEXT = "Only a department manager may approve finance access.";
const CHUNK_ID = "00000000-0000-4000-8000-000000000abc";

type HistoryMessage = { role: string; content: string };

/** One frame exactly as `agent_api.sse.encode_event` writes it. */
function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function citation(): Record<string, unknown> {
  return {
    document: "fake-access-policy",
    title: CITATION_TITLE,
    version: "v3",
    heading_path: ["Fake Section", "Fake Subsection"],
    chunk_id: CHUNK_ID,
    text: CITATION_TEXT,
    score: 0.42,
  };
}

/** A stream whose frames this test pushes one at a time, as a server would. */
function scriptedStream() {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(streamController) {
      controller = streamController;
    },
  });
  return {
    response(): Response {
      return new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    },
    push(event: string, data: unknown): void {
      controller?.enqueue(encoder.encode(frame(event, data)));
    },
    close(): void {
      controller?.close();
    },
  };
}

const fetchStub = vi.fn<typeof fetch>();

let historyMessages: HistoryMessage[] = [];
let streamAnswer: () => Response = () => new Response(null, { status: 200 });
let streamCalls = 0;

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  return input instanceof URL ? input.toString() : input.url;
}

function renderPanel(): void {
  render(
    <QueryProvider>
      <ChatPanel />
    </QueryProvider>,
  );
}

async function send(text = "Can I get the finance dashboard?"): Promise<void> {
  const user = userEvent.setup();
  await user.type(await screen.findByRole("textbox", { name: "Message" }), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => {
    expect(streamCalls).toBe(1);
  });
}

function conversation(): HTMLElement {
  return screen.getByRole("log", { name: "Conversation" });
}

function assistantMessages(): HTMLElement[] {
  return within(conversation())
    .queryAllByRole("article")
    .filter((element) => element.getAttribute("data-role") === "assistant");
}

beforeEach(() => {
  historyMessages = [];
  streamCalls = 0;
  streamAnswer = () => new Response(null, { status: 200 });
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes("/api/chat/messages")) {
      return new Response(JSON.stringify({ messages: historyMessages }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/api/chat/stream")) {
      streamCalls += 1;
      return streamAnswer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ChatPanel — history", () => {
  it("renders this session's messages oldest first, users and assistants apart", async () => {
    // Catches a panel that renders the history newest first (the shape a
    // "recent messages" read suggests), which shows a conversation reading
    // backwards, and one that renders every row identically, so a user cannot
    // tell their own question from the answer. `list_recent_messages` orders by
    // `ordinal`, so the wire order IS the display order and reversing it is the
    // implementation this kills.
    historyMessages = [
      { role: "user", content: "Can I get the finance dashboard?" },
      { role: "assistant", content: "Checking the policy." },
      { role: "user", content: "Thanks." },
    ];

    renderPanel();

    const messages = within(await screen.findByRole("log", { name: "Conversation" })).getAllByRole(
      "article",
    );
    expect(messages.map((element) => element.getAttribute("data-role"))).toEqual([
      "user",
      "assistant",
      "user",
    ]);
    expect(messages[0]).toHaveTextContent("Can I get the finance dashboard?");
    expect(messages[1]).toHaveTextContent("Checking the policy.");
    expect(messages[2]).toHaveTextContent("Thanks.");
  });
});

describe("ChatPanel — the streamed turn", () => {
  it("renders token events progressively and settles on the reply text", async () => {
    // Two kills in one arrangement. The mid-stream assertion catches a panel
    // that buffers every frame and renders once at `reply` — a turn then shows
    // nothing at all for its whole duration, which is the entire point of the
    // stream. The final assertion catches a panel that APPENDS the reply beside
    // the accumulated tokens, leaving the same sentence in the log twice.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("token", { text: "Checking " });
    expect(await screen.findByText(/Checking/)).toBeInTheDocument();

    stream.push("token", { text: "the policy." });
    expect(await screen.findByText("Checking the policy.")).toBeInTheDocument();

    stream.push("reply", {
      reply: "Checking the policy.",
      outcome: "done",
      citations: [],
      request_id: null,
    });
    stream.push("done", {});
    stream.close();

    await waitFor(() => {
      expect(assistantMessages()).toHaveLength(1);
    });
    expect(assistantMessages()[0]).toHaveTextContent("Checking the policy.");
  });

  it("settles on the reply event's string when it differs from the tokens", async () => {
    // The reconciliation §5 names. On the wire the tokens are always slices of
    // the same checked reply, so an on-contract script cannot tell a panel that
    // RECONCILES from one that just stops appending — this script therefore
    // diverges deliberately, which is the only arrangement in which removing
    // the reconciliation flips the result. Catches a panel that keeps its
    // token buffer as the final message and so would show a composition the
    // post-check replaced.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("token", { text: "an unchecked first attempt" });
    expect(await screen.findByText("an unchecked first attempt")).toBeInTheDocument();

    stream.push("reply", {
      reply: "the checked reply",
      outcome: "done",
      citations: [],
      request_id: null,
    });
    stream.push("done", {});
    stream.close();

    await waitFor(() => {
      expect(assistantMessages()[0]).toHaveTextContent("the checked reply");
    });
    expect(screen.queryByText("an unchecked first attempt")).toBeNull();
  });
});

describe("ChatPanel — the tool-step timeline", () => {
  it("renders one row per step event, naming each tool and its result status", async () => {
    // Two step events, carrying three tool results in total (one in the first
    // round, two in the second). Catches a panel that renders the state's whole
    // accumulated `tool_results` per step — the second row would then show four
    // entries instead of two — and one that shows a tool without its outcome,
    // so a refused `create_access_request` reads exactly like a successful one.
    // The typed code is what a user can act on; "failed" alone is not.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("step", {
      node: "retrieve",
      round: 1,
      tools: [{ tool: "search_policy", ok: true, error_code: null }],
    });
    stream.push("step", {
      node: "execute_tools",
      round: 2,
      tools: [
        { tool: "create_access_request", ok: false, error_code: "POLICY_BLOCKED" },
        { tool: "classify_access_request", ok: true, error_code: null },
      ],
    });

    const timeline = await screen.findByRole("list", { name: "Tool steps" });
    await waitFor(() => {
      expect(within(timeline).getAllByRole("listitem")).toHaveLength(2);
    });

    const rows = within(timeline).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("retrieve");
    expect(rows[0]).toHaveTextContent("1");
    expect(rows[0]).toHaveTextContent("search_policy");
    expect(rows[0]).toHaveTextContent("ok");
    expect(rows[1]).toHaveTextContent("execute_tools");
    expect(rows[1]).toHaveTextContent("2");
    expect(rows[1]).toHaveTextContent("create_access_request");
    expect(rows[1]).toHaveTextContent("POLICY_BLOCKED");
    expect(rows[1]).toHaveTextContent("classify_access_request");

    stream.close();
  });
});

describe("ChatPanel — citations", () => {
  it("shows title, version, heading path and score, and the chunk text only when expanded", async () => {
    // Catches a citation list that renders the chunk text inline: a settled
    // reply then carries several hundred words of policy prose the user did not
    // ask to read, which is exactly why §1 makes it expandable. The
    // before-click assertion is the load-bearing half — a list that shows
    // everything always would pass the after-click one.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("reply", {
      reply: "You will need manager approval.",
      outcome: "done",
      citations: [citation()],
      request_id: null,
    });
    stream.push("done", {});
    stream.close();

    const toggle = await screen.findByRole("button", { name: CITATION_TITLE });
    expect(screen.getByText(/v3/)).toBeInTheDocument();
    expect(screen.getByText(/Fake Section/)).toBeInTheDocument();
    expect(screen.getByText(/Fake Subsection/)).toBeInTheDocument();
    expect(screen.getByText(/0\.42/)).toBeInTheDocument();
    expect(screen.queryByText(CITATION_TEXT)).toBeNull();

    await userEvent.setup().click(toggle);

    expect(await screen.findByText(CITATION_TEXT)).toBeInTheDocument();
  });
});

describe("ChatPanel — an error event", () => {
  it("renders one typed failure, drops the partial text and leaves the composer enabled", async () => {
    // The partial token before the error is what makes this a real case: it
    // catches a panel that leaves a half-composed sentence standing as if the
    // turn had answered it, which is prose about a turn nothing decided
    // (`_failure_frames` sends no `token` and no `reply` with an error for the
    // same reason). The single-alert assertion catches a panel that renders one
    // failure per frame, and the enabled composer catches one that treats a
    // failed turn as a permanent state and locks the user out of retrying.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("token", { text: "a half composed sentence" });
    expect(await screen.findByText("a half composed sentence")).toBeInTheDocument();

    stream.push("error", { code: "INTERNAL" });
    stream.push("done", {});
    stream.close();

    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent(FAILURE_MESSAGES.INTERNAL ?? "no INTERNAL entry");
    expect(screen.queryByText("a half composed sentence")).toBeNull();
    expect(assistantMessages()).toHaveLength(0);
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });
});

describe("ChatPanel — a refusal that arrives before the stream", () => {
  const rows = [
    { name: "the rate limit", status: 429, code: "POLICY_BLOCKED" },
    { name: "the message cap", status: 400, code: "VALIDATION" },
  ] as const;

  it.each(rows)(
    "$name renders its own typed message and opens no stream",
    async ({ status, code }) => {
      // Catches a panel that pipes any response body into the frame parser
      // regardless of status — the refusal is a JSON envelope, so the user would
      // see a turn that started and silently produced nothing — and one that
      // renders a single generic sentence for every failure, which cannot tell a
      // user to wait a moment from one to shorten their message. The code is read
      // from `{ code }`, agent-api's own envelope, so a panel looking for
      // `{ message }` renders the default and fails here.
      streamAnswer = () =>
        new Response(JSON.stringify({ code }), {
          status,
          headers: { "content-type": "application/json" },
        });
      renderPanel();
      await send();

      const alerts = await screen.findAllByRole("alert");
      expect(alerts).toHaveLength(1);
      expect(alerts[0]).toHaveTextContent(FAILURE_MESSAGES[code] ?? `no ${code} entry`);
      expect(assistantMessages()).toHaveLength(0);
      expect(streamCalls).toBe(1);
      expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
    },
  );
});
