/**
 * The parser for what `/api/me` returns.
 *
 * `tabs` is a list of stable keys the server computed from the RBAC matrix;
 * the web owns the names and hrefs (`components/nav.tsx`). Keys are plain
 * strings so a key a newer server adds is ignored by an older shell instead
 * of refusing the whole payload. Nothing here is `.strict()` for the same
 * reason as `access-schema.ts`.
 */

import { z } from "zod";

export const meSchema = z.object({
  display_name: z.string(),
  principals: z.array(z.string()),
  tabs: z.array(z.string()),
});

export type Me = z.infer<typeof meSchema>;
