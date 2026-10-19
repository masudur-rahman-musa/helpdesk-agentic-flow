/**
 * The chat stream shapes and the readers that narrow them. Every frame
 * arrives as `unknown`, and each reader returns the type or `null`, so a
 * frame the server changes costs one row and never a crashed render.
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

/** One seat in the approval chain: the slot, and whether a person fills it. */
export type CardApprover = { slot: string; resolved: boolean };

/**
 * The turn's decision card. `requestId`, `requestNumber` and `status` are null together when
 * nothing was filed. Enum fields stay strings, so a new server value shows as
 * itself instead of failing to parse.
 */
export type DecisionCard = {
  requestId: string | null;
  /** What a person sees, like REQ-0107; the id never reaches the screen. */
  requestNumber: string | null;
  status: string | null;
  decision: string;
  riskClass: string;
  reasons: string[];
  approvers: CardApprover[];
  effectiveExpiryDays: number;
  expiresAt: string | null;
};

/** One line of the chat list. */
export type ChatSummary = { id: string; title: string; lastSeenAt: string };

export type ReplyEvent = {
  reply: string;
  citations: Citation[];
  decision: DecisionCard | null;
  chatId: string | null;
};

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

/**
 * The `decision` of a `reply` frame, or `null` when the turn decided nothing.
 * Narrowed, never cast: a cast would let a bad shape throw inside render and
 * take the whole conversation down.
 */
export function readDecision(data: unknown): DecisionCard | null {
  const card = record(data);
  if (card === null) {
    return null;
  }
  const approvers: CardApprover[] = [];
  for (const entry of list(card.approvers)) {
    const approver = record(entry);
    if (approver === null) {
      continue;
    }
    approvers.push({ slot: text(approver.slot), resolved: approver.resolved === true });
  }
  return {
    requestId: typeof card.request_id === "string" ? card.request_id : null,
    requestNumber: typeof card.request_number === "string" ? card.request_number : null,
    status: typeof card.status === "string" ? card.status : null,
    decision: text(card.decision),
    riskClass: text(card.risk_class),
    reasons: list(card.reasons).map(text),
    approvers,
    effectiveExpiryDays:
      typeof card.effective_expiry_days === "number" ? card.effective_expiry_days : 0,
    expiresAt: typeof card.expires_at === "string" ? card.expires_at : null,
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
  return {
    reply: text(frame.reply),
    citations,
    decision: readDecision(frame.decision),
    chatId: typeof frame.chat_id === "string" ? frame.chat_id : null,
  };
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

/** `{ chats: [{ id, title, last_seen_at }] }`, in the order the route sent it. */
export function readChatList(body: unknown): ChatSummary[] {
  const chats: ChatSummary[] = [];
  for (const entry of list(record(body)?.chats)) {
    const chat = record(entry);
    if (chat === null || typeof chat.id !== "string") {
      continue;
    }
    chats.push({ id: chat.id, title: text(chat.title), lastSeenAt: text(chat.last_seen_at) });
  }
  return chats;
}
