import { ToggleRefusedError } from "@/features/admin/killswitch/api";
import { failureMessage } from "@/lib/failure-messages";

/** The kill switch page's sentences. */

/** Names what failed: the page's other reading is "all clear". */
export const KILL_SWITCH_READ_MESSAGE =
  "The kill switches could not be read. Try again in a moment.";

/** Says the change did not land, which is what an operator needs to know. */
export const KILL_SWITCH_TOGGLE_MESSAGE =
  "The kill switch could not be changed. Try again in a moment.";

export const KILL_SWITCH_REASON_REQUIRED_MESSAGE = "Give a reason before changing a kill switch.";

/** The shared sentence is about chat rate limits; here the block is permanent. */
export const KILL_SWITCH_POLICY_BLOCKED_MESSAGE = "That tool is protected and cannot be stopped.";

/** Here the only typed value is the reason, so the sentence names it. */
export const KILL_SWITCH_VALIDATION_MESSAGE =
  "That could not be saved. Shorten the reason and try again.";

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
