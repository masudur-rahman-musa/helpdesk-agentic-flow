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
});

export type Me = z.infer<typeof meSchema>;
