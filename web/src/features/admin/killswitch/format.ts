import { ToggleRefusedError } from "@/features/admin/killswitch/api";
import { failureMessage } from "@/lib/failure-messages";

/**
 * The sentence a failed `/admin/killswitch` read shows, and the
 * four sentences a toggle shows, moved here from
 * `components/kill-switch-toggle.tsx`.
 *
 * The switch instant's renderer moved to `lib/date-format.ts`: the
 * policies board and the kill-switch board both use it.
 *
 * Pinned by `tests/features/admin/killswitch/killswitch-format.test.ts`.
 */

/**
 * The sentence for a read that did not come back as a board (decision E).
 *
 * Its own string rather than `FAILURE_MESSAGES.default`, which says only that
 * something went wrong and nothing about WHICH thing could not be read — on a
 * page whose alternative reading is "the platform is clean". Pinned verbatim by
 * `tests/features/admin/killswitch/killswitch-format.test.ts::KILL_SWITCH_READ_MESSAGE`.
 */
export const KILL_SWITCH_READ_MESSAGE =
  "The kill switches could not be read. Try again in a moment.";

/**
 * The sentence for a toggle whose answer said nothing usable (decision F).
 *
 * Its own string rather than `FAILURE_MESSAGES.default`: on this surface the
 * question an operator has is whether the stop LANDED, and "something went
 * wrong" leaves it open. Pinned verbatim by
 * `tests/killswitch-toggle.test.tsx::pins the failure and the reason-required
 * sentences exactly`.
 */
export const KILL_SWITCH_TOGGLE_MESSAGE =
  "The kill switch could not be changed. Try again in a moment.";

/** The sentence the page says when a control is clicked with no reason typed. */
export const KILL_SWITCH_REASON_REQUIRED_MESSAGE = "Give a reason before changing a kill switch.";

/**
 * The `POLICY_BLOCKED` sentence on this surface, and only on this surface
 * (decision E).
 *
 * `FAILURE_MESSAGES.POLICY_BLOCKED` is about chat rate limiting ("Too many
 * messages just now."), which would tell an admin to wait for a refusal that is
 * a permanent property of a protected tool. 22b's `tests/chat-schema.test.ts`
 * pins every string in that map verbatim, so widening the shared map is not
 * available. Pinned by `::pins the two override sentences exactly and apart
 * from the shared map`.
 */
export const KILL_SWITCH_POLICY_BLOCKED_MESSAGE = "That tool is protected and cannot be stopped.";

/**
 * The `VALIDATION` sentence on this surface, and only on this surface
 * (decision E).
 *
 * The shared one is "That message could not be sent.", which is about a chat
 * message; here the only value a person typed is the reason, so the sentence
 * says which one to shorten. Exactly TWO codes are overridden — every other
 * sentence still comes from the shared map, so no second opinion about
 * `FORBIDDEN` or `TOOL_DISABLED` can appear on this page.
 */
export const KILL_SWITCH_VALIDATION_MESSAGE =
  "That could not be saved. Shorten the reason and try again.";

/** The sentence for a refused toggle, or `null` while nothing is refused. */
export function toggleRefusal(error: unknown): string | null {
  if (error === null || error === undefined) {
    return null;
  }
  if (!(error instanceof ToggleRefusedError)) {
    return KILL_SWITCH_TOGGLE_MESSAGE;
  }
  if (error.code === "POLICY_BLOCKED") {
    return KILL_SWITCH_POLICY_BLOCKED_MESSAGE;
  }
  if (error.code === "VALIDATION") {
    return KILL_SWITCH_VALIDATION_MESSAGE;
  }
  return failureMessage(error.code);
}
