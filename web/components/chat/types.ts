/**
 * The wire shapes this surface reads, and the readers that narrow them.
 *
 * Everything crossing the SSE boundary arrives as `unknown`: a frame's payload
 * is whatever `JSON.parse` produced, and retrieved policy text in particular is
 * DATA, never instructions and never a shape to be trusted. Each reader takes
 * `unknown` and returns either a value of the declared type or `null`, so a
 * frame the server changes under us costs its own row and never a thrown
 * render.
 *
 * The shapes are agent-api's as BUILT, not as remembered:
 * `step` → `{node, round, tools:[{tool, ok, error_code}]}`
 * (`graph/build.py::_step_data`), `reply` → `{reply, outcome, citations,
 * request_id}` (`::_reply_data`), `error` → `{code}` (`::_failure_frames`),
 * a citation → `graph/state.py::Citation`, and the history →
 * `chat_routes.ChatHistoryResponse`. Asserted against those literal shapes by
 * `tests/chat-page.test.tsx`.
 */

export type ChatRole = "user" | "assistant";

export type ChatMessage = { role: ChatRole; content: string };

export type ToolResult = { tool: string; ok: boolean; errorCode: string | null };

export type StepEvent = { node: string; round: number; tools: ToolResult[] };

export type Citation = {
  document: string;
  title: string;
  version: string;
  headingPath: string[];
  chunkId: string;
  text: string;
  score: number;
};

export type ReplyEvent = { reply: string; citations: Citation[] };

function record(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** The `text` of a `token` payload, or "" — a frame with none adds nothing. */
export function readToken(data: unknown): string {
  return text(record(data)?.text);
}

/** The typed code of an `error` frame or a refusal body, as a string. */
export function readCode(data: unknown): string {
  return text(record(data)?.code);
}

export function readStep(data: unknown): StepEvent | null {
  const frame = record(data);
  if (frame === null) {
    return null;
  }
  const tools: ToolResult[] = [];
  for (const entry of list(frame.tools)) {
    const tool = record(entry);
    if (tool === null) {
      continue;
    }
    tools.push({
      tool: text(tool.tool),
      ok: tool.ok === true,
      errorCode: typeof tool.error_code === "string" ? tool.error_code : null,
    });
  }
  return {
    node: text(frame.node),
    round: typeof frame.round === "number" ? frame.round : 0,
    tools,
  };
}

function readCitation(value: unknown): Citation | null {
  const citation = record(value);
  if (citation === null) {
    return null;
  }
  return {
    document: text(citation.document),
    title: text(citation.title),
    version: text(citation.version),
    headingPath: list(citation.heading_path).map(text),
    chunkId: text(citation.chunk_id),
    text: text(citation.text),
    score: typeof citation.score === "number" ? citation.score : 0,
  };
}

export function readReply(data: unknown): ReplyEvent | null {
  const frame = record(data);
  if (frame === null) {
    return null;
  }
  const citations: Citation[] = [];
  for (const entry of list(frame.citations)) {
    const citation = readCitation(entry);
    if (citation !== null) {
      citations.push(citation);
    }
  }
  // A paused turn carries its pending sentence in the same key, and a turn
  // that failed sends an `error` frame instead of this one, so `reply` is the
  // settled text whatever the outcome.
  return { reply: text(frame.reply), citations };
}

/** `{ messages: [{ role, content }] }`, in the order the route sent it. */
export function readHistory(body: unknown): ChatMessage[] {
  const messages: ChatMessage[] = [];
  for (const entry of list(record(body)?.messages)) {
    const message = record(entry);
    if (message === null) {
      continue;
    }
    messages.push({
      role: message.role === "user" ? "user" : "assistant",
      content: text(message.content),
    });
  }
  return messages;
}
