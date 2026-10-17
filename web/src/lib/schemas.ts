/**
 * The one login schema, shared by the form and the route handler. The widths
 * match the database columns. Patterns are anchored at both ends, or a
 * trailing newline would pass. A pre-check only: it keeps oversized bodies from
 * agent-api, which still checks on its own.
 */

import { z } from "zod";

export const SLUG_MAX_LENGTH = 64;
export const EMAIL_MAX_LENGTH = 320;

export const SLUG_PATTERN = /^[a-z0-9-]+$/;

/**
 * Simpler than a full address grammar on purpose: one `@`, a dot in the
 * domain, no whitespace.
 */
export const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/** One message for every refusal, so nothing names which input was wrong. */
export const SIGN_IN_FAILED_MESSAGE = "Sign-in failed. Check your details and try again.";

export const loginSchema = z.object({
  tenant_slug: z.string().min(1).max(SLUG_MAX_LENGTH).regex(SLUG_PATTERN),
  email: z.string().min(1).max(EMAIL_MAX_LENGTH).regex(EMAIL_PATTERN),
  password: z.string().min(1),
});

export type LoginInput = z.infer<typeof loginSchema>;
