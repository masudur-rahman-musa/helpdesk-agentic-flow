/**
 * The one chat message schema, shared by the composer and the route handler
 *.
 *
 * `MAX_MESSAGE_CHARS` mirrors `helpdesk.models.conversation.MAX_MESSAGE_CHARS`,
 * which is the `messages.content` column width itself. The three answers here
 * are the three `check_message_content` gives — the empty message refused, a
 * message of exactly the cap accepted, one character over refused — pinned on
 * both sides of the bound by `tests/chat-schema.test.ts` ("pins the cap at
 * exactly 4000" and its four-row boundary table).
 *
 * This is a pre-check, not the gate. `check_message_content` runs again inside
 * agent-api before any row is written, and any other client can still post an
 * uncapped body; what the pre-check buys is a refusal the composer can render
 * without spending a round trip and a rate-limit slot on a message the client
 * already knew was unsendable (`tests/chat-stream-route.test.ts` asserts the
 * fetch call count, primed to succeed, which is the only assertion that can
 * tell this order from the other one).
 *
 * The schema is an object over the WIRE body rather than a bare string, so the
 * composer and the handler cannot drift into checking different shapes: one
 * `safeParse` serves both. A non-string `message` is refused here rather than
 * coerced, because `String(body.message).length` passes every boundary row and
 * forwards a body agent-api answers 422 to, which carries no typed code the
 * client could render (`tests/chat-schema.test.ts`, "refuses a body whose
 * message is not a string").
 */

import { z } from "zod";

/** The `messages.content` column width, mirrored from the Python constant. */
export const MAX_MESSAGE_CHARS = 4000;

export const chatMessageSchema = z.object({
  message: z.string().min(1).max(MAX_MESSAGE_CHARS),
});

export type ChatMessageInput = z.infer<typeof chatMessageSchema>;
