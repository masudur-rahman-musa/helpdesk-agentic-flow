import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPanel } from "@/components/chat/chat-panel";
import { QueryProvider } from "@/components/query-provider";
import { FAILURE_MESSAGES } from "@/lib/chat-errors";
import { CHAT_PREFILL_PARAM } from "@/lib/chat-prefill";
import { MAX_MESSAGE_CHARS } from "@/lib/chat-schema";
import { reasonSentence } from "@/lib/decision-labels";

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
 * Shapes an earlier change's §6 left open, fixed here for the same reason (standing rule
 * 55/39; these go in 22d's Deviations):
 * - the decision card is a `region` whose accessible name is exactly `Decision`
 *   (§6 fixes this one), and the approver chain inside it is a `list` whose
 *   accessible name is `Approvers` — the shape `ToolSteps` already uses, and
 *   the only way "each approver is exactly one `listitem`" is countable in a
 *   region that may hold other lists (the reasons).
 * - `request_id` rides as the `data-request-id` attribute ON the region
 *   element itself (§1), so a classified-only card is the ABSENCE of that
 *   attribute, not an empty one.
 * - the reason sentences and the four label lookups are
 *   `@/lib/decision-labels`'; the sentences are compared by reference here and
 *   pinned as literals in `tests/decision-labels.test.ts`, the same division
 *   `FAILURE_MESSAGES` already has with `tests/chat-schema.test.ts`.
 *
 * The wire shapes are agent-api's as BUILT, not as remembered:
 * `step` → `{node, round, tools:[{tool, ok, error_code}]}` (`graph/build.py::_step_data`),
 * `reply` → `{reply, outcome, citations, request_id, decision}` (`::_reply_data`);
 * `decision` is the turn's decision card or null — `{request_id,
 * status, decision, risk_class, reasons, approvers:[{slot, resolved}],
 * effective_expiry_days, expires_at}`, `agent_api/decision_card.py::DecisionCard`
 * dumped `mode="json"` — rendered by `ChatPanel` as the `Decision` region since
 * an earlier change,
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

describe("ChatPanel — the decision card", () => {
  const REQUEST_ID = "11111111-0000-4000-8000-0000000000aa";
  const RULE_ID = "22222222-0000-4000-8000-0000000000bb";

  /** `DecisionCard` as `_reply_data` dumps it: a filed request, still pending. */
  function filedCard(): Record<string, unknown> {
    return {
      request_id: REQUEST_ID,
      status: "pending",
      decision: "approval",
      risk_class: "privileged",
      reasons: ["NOT_IN_TEMPLATE", `SOD_CONFLICT:${RULE_ID}`],
      approvers: [
        { slot: "department_manager", resolved: true },
        { slot: "it_admin", resolved: false },
      ],
      effective_expiry_days: 90,
      expires_at: "2026-12-01T09:30:00Z",
    };
  }

  /** The same card for a turn that classified and filed nothing (22c decision B). */
  function classifiedCard(): Record<string, unknown> {
    return {
      request_id: null,
      status: null,
      decision: "auto",
      risk_class: "low",
      reasons: ["IN_TEMPLATE"],
      approvers: [{ slot: "department_manager", resolved: false }],
      effective_expiry_days: 30,
      expires_at: null,
    };
  }

  /** A settled `reply` frame carrying whatever the server put in `decision`. */
  function reply(decision: unknown, outcome = "await_approval"): Record<string, unknown> {
    return {
      reply: "You will need manager approval.",
      outcome,
      citations: [],
      request_id: outcome === "await_approval" ? REQUEST_ID : null,
      decision,
    };
  }

  it("renders the decision, risk, reasons, expiry, approvers and status of a filed request", async () => {
    // §1's first criterion, all six fields in one arrangement. Catches a card
    // that shows the wire values instead of the labels (`approval`,
    // `privileged`, `pending` at the user), one that renders the reason CODES
    // where a sentence belongs, and one that formats `expires_at` through the
    // browser locale — `toLocaleDateString` under jsdom answers "12/1/2026",
    // which is a different date in half the world, so the ISO prefix is the
    // only unambiguous thing to print. The two seats are one resolved and one
    // not, so an implementation ignoring `resolved` fails whichever word it
    // hardcodes: "assigned" everywhere fails the second seat, "not yet
    // assigned" everywhere fails the first. The two uuid assertions catch a
    // card that prints the SoD rule id or the request id as text (decisions C
    // and §1) — a raw uuid reads as nothing to the person asking.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("reply", reply(filedCard()));
    stream.push("done", {});
    stream.close();

    const region = await screen.findByRole("region", { name: "Decision" });
    expect(region).toHaveTextContent("Needs approval");
    expect(region).toHaveTextContent("Privileged");
    expect(region).toHaveTextContent("Pending approval");
    expect(region).toHaveTextContent(reasonSentence("NOT_IN_TEMPLATE"));
    expect(region).toHaveTextContent(reasonSentence("SOD_CONFLICT"));
    expect(region).toHaveTextContent("Expires in 90 days");
    expect(region).toHaveTextContent("2026-12-01");
    expect(region).toHaveAttribute("data-request-id", REQUEST_ID);

    const seats = within(within(region).getByRole("list", { name: "Approvers" })).getAllByRole(
      "listitem",
    );
    expect(seats).toHaveLength(2);
    expect(seats[0]).toHaveTextContent("Department manager");
    expect(seats[0]).toHaveTextContent("assigned");
    expect(seats[0]).not.toHaveTextContent("not yet assigned");
    expect(seats[1]).toHaveTextContent("IT admin");
    expect(seats[1]).toHaveTextContent("not yet assigned");

    expect(region.textContent ?? "").not.toContain(RULE_ID);
    expect(region.textContent ?? "").not.toContain(REQUEST_ID);
  });

  it("a classified request shows no status line", async () => {
    // The turn that classified and filed nothing: `request_id` and `status`
    // are both null and the other six fields are answered exactly as they are
    // for a filed request. Catches a card that renders the status row
    // unconditionally — `STATUS_LABELS[null]` is the literal "undefined" on
    // screen, and a default of "Pending approval" would tell a user something
    // was filed for them when nothing was. The absent `data-request-id`
    // catches an attribute written as `String(request_id)`, which row 23 would
    // then read the string "null" out of, and the date assertion catches an
    // expiry line built from a null `expires_at`.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("reply", reply(classifiedCard(), "done"));
    stream.push("done", {});
    stream.close();

    const region = await screen.findByRole("region", { name: "Decision" });
    expect(region).toHaveTextContent("Granted automatically");
    expect(region).toHaveTextContent("Low risk");
    expect(region).toHaveTextContent(reasonSentence("IN_TEMPLATE"));
    expect(region).toHaveTextContent("Expires in 30 days");
    expect(
      within(within(region).getByRole("list", { name: "Approvers" })).getAllByRole("listitem"),
    ).toHaveLength(1);

    expect(region).not.toHaveAttribute("data-request-id");
    expect(region).not.toHaveTextContent(
      /Pending approval|Approved|Rejected|Access granted|Failed|undefined|null/,
    );
    expect(region).not.toHaveTextContent(/\d{4}-\d{2}-\d{2}/);
  });

  it("a turn with no decision renders no card", async () => {
    // The common turn: a policy question decided nothing about access, so
    // `decision_card` answered `None` and the wire carries `decision: null`.
    // Catches a panel that builds a card from every settled reply — an empty
    // region with a blank decision word, a blank risk and "Expires in
    // undefined days" under every answer the assistant gives. The assistant
    // message assertion is what proves the frame was applied at all, so an
    // absent region cannot pass by the reply never arriving.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("reply", reply(null, "done"));
    stream.push("done", {});
    stream.close();

    await waitFor(() => {
      expect(assistantMessages()).toHaveLength(1);
    });
    expect(screen.queryByRole("region", { name: "Decision" })).toBeNull();
  });

  it("a decision that is not an object renders no card", async () => {
    // The same absence, reached by a different route: a server that one day
    // sends the decision WORD in that key instead of the card. Catches the
    // reader written as `if (frame.decision !== null) setCard(frame.decision as
    // DecisionCard)` — a cast is not a check, so `card.approvers.map` throws
    // inside render and the whole panel unmounts, taking the conversation with
    // it. That implementation passes the null case above, which is why this
    // row exists separately: `readDecision` must narrow the SHAPE, not test
    // for null.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("reply", reply("approval", "done"));
    stream.push("done", {});
    stream.close();

    await waitFor(() => {
      expect(assistantMessages()).toHaveLength(1);
    });
    expect(screen.queryByRole("region", { name: "Decision" })).toBeNull();
    expect(assistantMessages()[0]).toHaveTextContent("You will need manager approval.");
  });

  it("an approver whose slot is unknown renders the raw slot", async () => {
    // §1's enum-drift path, on the map whose member list is most likely to
    // grow. Catches the bare index — an `ApproverSlot` added on the Python
    // side renders "undefined" against a seat somebody has to fill — and a
    // card that DROPS a seat it cannot label, which would show a two-approver
    // chain as one and tell the user less than the engine decided. The seat
    // count is the half that fails for the dropping implementation.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push(
      "reply",
      reply({ ...filedCard(), approvers: [{ slot: "chief_of_staff", resolved: false }] }),
    );
    stream.push("done", {});
    stream.close();

    const region = await screen.findByRole("region", { name: "Decision" });
    const seats = within(within(region).getByRole("list", { name: "Approvers" })).getAllByRole(
      "listitem",
    );
    expect(seats).toHaveLength(1);
    expect(seats[0]).toHaveTextContent("chief_of_staff");
    expect(seats[0]).not.toHaveTextContent("undefined");
  });

  it("an error event clears the card", async () => {
    // The card has the same lifetime the steps and the citations already have
    // (decision D). Catches a panel that leaves the previous card standing
    // through a failure: the user then reads a decision beside a sentence
    // saying the turn decided nothing, which is the same class of lie as the
    // half-composed token text the error path already drops. The
    // single-alert assertion is carried over from the error case above so a
    // panel that clears the card by re-rendering a second failure cannot pass.
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();
    await send();

    stream.push("reply", reply(filedCard()));
    await screen.findByRole("region", { name: "Decision" });

    stream.push("error", { code: "INTERNAL" });
    stream.push("done", {});
    stream.close();

    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Decision" })).toBeNull();
    });
    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent(FAILURE_MESSAGES.INTERNAL ?? "no INTERNAL entry");
  });

  it("a second send clears the previous turn's card", async () => {
    // The other half of decision D, and the one a panel passes by accident:
    // clearing on `reply` alone leaves the previous turn's decision on screen
    // for the whole of the next turn, which is precisely when a user is
    // waiting to see whether THIS request was approved. Asserted after the
    // second submit and BEFORE the second stream sends anything, so an
    // implementation that overwrites the card at the next `reply` still fails.
    // The first stream is closed first because `streaming.current` gates the
    // second send until the first body is drained.
    const first = scriptedStream();
    const second = scriptedStream();
    streamAnswer = () => (streamCalls === 1 ? first.response() : second.response());
    renderPanel();
    await send();

    first.push("reply", reply(filedCard()));
    first.push("done", {});
    first.close();
    await screen.findByRole("region", { name: "Decision" });

    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "Message" }), "And the HR system?");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => {
      expect(streamCalls).toBe(2);
    });

    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Decision" })).toBeNull();
    });

    second.close();
  });
});

/**
 * An earlier change §6 additions: the composer's prefill from `/access`' renewal link.
 * Additions only — nothing above this comment is edited.
 *
 * Shapes this file fixes that 23b left open (a project rule and 55; these
 * belong in 23b's Deviations):
 * - the panel reads `window.location.search` once on mount (§2, decision C),
 *   so the parameter is set here with `history.replaceState` — no router
 *   context, no Suspense boundary and no navigation, which is exactly the cost
 *   `useSearchParams` would have added to every case above.
 * - the prefilled text is the textarea's VALUE, not a placeholder: a
 *   placeholder is not sent when the user presses Send, which would make the
 *   link look like it worked and send nothing.
 */
describe("ChatPanel — the renewal prefill", () => {
  const ASK = "Can I renew Fake Payroll Read on Fake Payroll?";

  function setSearch(search: string): void {
    window.history.replaceState({}, "", `/chat${search}`);
  }

  function composer(): Promise<HTMLElement> {
    return screen.findByRole("textbox", { name: "Message" });
  }

  afterEach(() => {
    setSearch("");
  });

  it("starts the composer carrying the ask and sends nothing until Send is pressed", async () => {
    // Two kills in one arrangement. The value assertion catches a panel that
    // ignores `?ask=` altogether — `/access`' renewal link then lands on an
    // empty composer and the user retypes the sentence, which is the whole of
    // decision C. The `streamCalls === 0` assertion is the load-bearing one: it
    // catches a panel that SENDS the prefill on mount, which would let a link
    // in an email open a turn the person never asked for (a project rule's
    // boundary is the mutation, but the turn is still theirs to start). The
    // body assertion afterwards catches a prefill rendered as a PLACEHOLDER:
    // the text is on screen, Send posts an empty message, and the user sees a
    // VALIDATION refusal for a sentence they can read in front of them.
    setSearch(`?${CHAT_PREFILL_PARAM}=${encodeURIComponent(ASK)}`);
    const stream = scriptedStream();
    streamAnswer = () => stream.response();
    renderPanel();

    await waitFor(async () => {
      expect(await composer()).toHaveValue(ASK);
    });
    expect(streamCalls).toBe(0);

    await userEvent.setup().click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => {
      expect(streamCalls).toBe(1);
    });

    const streamCall = fetchStub.mock.calls.find(([input]) =>
      String(input).includes("/api/chat/stream"),
    );
    expect(JSON.parse(String(streamCall?.[1]?.body ?? "{}"))).toEqual({ message: ASK });

    stream.close();
  });

  const EMPTY_STARTS = [
    { name: "no ask parameter at all", search: "" },
    { name: "another parameter", search: "?ref=email" },
    { name: "an empty ask", search: `?${CHAT_PREFILL_PARAM}=` },
    {
      name: "an ask one character over the cap",
      search: `?${CHAT_PREFILL_PARAM}=${"a".repeat(MAX_MESSAGE_CHARS + 1)}`,
    },
  ] as const;

  it.each(EMPTY_STARTS)("starts empty with $name", async ({ search }) => {
    // The first two rows catch a prefill that leaks a default — a remembered
    // draft or a sample question seeded into every visit to `/chat`, which
    // makes the composer's own emptiness unreliable. The last two are §1's
    // refusal, and they are the rows that fail for a panel skipping
    // `chatMessageSchema`: an oversized `ask` would seed a message the composer
    // itself refuses, so Send answers VALIDATION over text the app put there.
    setSearch(search);
    renderPanel();

    expect(await composer()).toHaveValue("");
    expect(streamCalls).toBe(0);
  });
});
