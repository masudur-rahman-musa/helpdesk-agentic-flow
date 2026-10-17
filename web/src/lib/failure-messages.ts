/**
 * One sentence per typed error code. The server sends only the code, so every
 * page says the same thing about one refusal. A sentence never names a tenant,
 * user or tool: it says what to do next, not what exists behind it.
 */

/** Every typed error code, as its wire value. */
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
 * The sentence for any reply, even one with no `code`, which falls to
 * `default` instead of an empty alert.
 */
export function failureMessage(code: unknown): string {
  if (typeof code === "string" && code in FAILURE_MESSAGES) {
    return FAILURE_MESSAGES[code as FailureCode];
  }
  return FAILURE_MESSAGES.default;
}
