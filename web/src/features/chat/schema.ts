/**
 * The one chat message schema, shared by the composer and the route handler.
 * A pre-check only: the server checks again. It saves a round trip and a
 * rate-limit slot on a message the client already knows it cannot send.
 */

import { z } from "zod";

/** The `messages.content` column width, mirrored from the Python constant. */
export const MAX_MESSAGE_CHARS = 4000;

export const chatMessageSchema = z.object({
  message: z.string().min(1).max(MAX_MESSAGE_CHARS),
});

export type ChatMessageInput = z.infer<typeof chatMessageSchema>;
