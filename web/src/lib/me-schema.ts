/**
 * The parser for `/api/me`. `tabs` are keys the server derives from roles; the
 * web owns their names and links. Plain strings, so a new key is ignored
 * instead of failing the whole payload.
 */

import { z } from "zod";

export const meSchema = z.object({
  display_name: z.string(),
  principals: z.array(z.string()),
  tabs: z.array(z.string()),
  /** A missing value means off, so an older answer never shows the steps. */
  show_tool_steps: z.boolean().default(false),
});

export type Me = z.infer<typeof meSchema>;
