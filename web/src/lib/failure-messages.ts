/**
 * One sentence per typed code, in one place.
 *
 * The wire carries `{ code: "<ErrorCode value>" }` — agent-api's own envelope,
 * from `chat_routes._typed` and `graph/build.py::_failure_frames` — and never
 * a human sentence: the design's vocabulary is closed precisely so the client
 * owns the wording and no free text crosses the boundary. Rendering the code
 * itself would show a user "POLICY_BLOCKED"; picking a sentence per call site
 * is how two places end up saying two different things about one refusal.
 *
 * The map is TOTAL over the eight codes in `helpdesk/errors.py::ErrorCode`
 * plus `default`, so a code that reaches the alert region always has a
 * sentence: `INTERNAL` in particular is what a mid-stream failure arrives as,
 * and a missing entry would render the literal "undefined". The three
 * sentences an earlier change fixed are pinned verbatim, and the totality of the map is
 * pinned through `INTERNAL`, by `tests/chat-schema.test.ts`.
 *
 * No sentence names a tenant, a user or a tool: a refusal explains what the
 * person can do next and nothing about what exists behind it.
 */

/** Every the design code, as its wire value. */
export type FailureCode =
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "SOD_CONFLICT"
  | "POLICY_BLOCKED"
  | "IDEMPOTENCY_CONFLICT"
  | "TOOL_DISABLED"
  | "VALIDATION"
  | "INTERNAL";

export const FAILURE_MESSAGES: Record<FailureCode | "default", string> = {
  FORBIDDEN: "You are not allowed to do that.",
  NOT_FOUND: "That could not be found.",
  SOD_CONFLICT: "That would conflict with access you already hold.",
  POLICY_BLOCKED: "Too many messages just now. Wait a moment and send it again.",
  IDEMPOTENCY_CONFLICT: "That request was already sent. Check your requests before sending again.",
  TOOL_DISABLED: "That is switched off right now. Try again later.",
  VALIDATION: "That message could not be sent.",
  INTERNAL: "Something went wrong. Nothing was changed.",
  default: "Something went wrong. Nothing was changed.",
};

/**
 * The sentence for whatever the wire said, including nothing at all: a body
 * with no `code` (a proxy's error page, a truncated JSON) falls to `default`
 * rather than rendering an empty alert.
 */
export function failureMessage(code: unknown): string {
  if (typeof code === "string" && code in FAILURE_MESSAGES) {
    return FAILURE_MESSAGES[code as FailureCode];
  }
  return FAILURE_MESSAGES.default;
}
